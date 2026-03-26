"""
World Registry — Dynamic Genre Orchestration
=============================================
The single entry point for all world/genre discovery, metadata loading,
and "manifesting" new RPG systems at runtime.

How it works
------------
1. Discovery (System Scan)
   WorldRegistry.scan() inspects every subdirectory of `data/fonts/`.
   Each subdirectory is treated as a world.  If a `world.json` exists
   inside, it is loaded as a WorldSchema.  If not, a minimal schema is
   auto-generated from the folder name.

2. Metadata Injection (The .json Ghost)
   `get_schema(world_name)` returns the in-memory cached WorldSchema for
   any discovered world.  The schema carries `gm_tone_block` — a
   formatted string that the GM Director injects into every narrative
   prompt, giving the AI the correct "vibe" for the active genre.

3. Manifesting New Worlds (Unsealed Command)
   `manifest(world_name)` is called when a player types `/switch_world`
   with a world name that does not yet exist on disk.  It creates:
     data/fonts/<world_name>/
     data/fonts/<world_name>/world.json  (minimal schema)
     data/handouts/<world_name>/
     data/echo_vault/<world_name>/
   and returns (schema, manifested=True).  Existing worlds return
   manifested=False.

Cache policy
------------
Schemas are cached in memory after the first load.  Call `reload(world_name)`
to force a re-read from disk (useful after editing world.json at runtime).
`scan()` (called at startup) populates the full cache.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.schemas.world_schema import WorldSchema

if TYPE_CHECKING:
    from orchestrator.services.reality_wall import RealityWall

logger = logging.getLogger(__name__)

_WORLD_JSON = "world.json"


class WorldRegistry:
    """
    Manages dynamic discovery and metadata for all RPG worlds/systems.

    Designed as a long-lived singleton wired into the FastAPI lifespan.
    Thread-safe for read access; write operations (manifest) are guarded
    by the RealityWall async lock.
    """

    def __init__(self, data_dir: str, reality_wall: "RealityWall") -> None:
        self._fonts_dir   = Path(data_dir) / "fonts"
        self._reality_wall = reality_wall
        self._cache: dict[str, WorldSchema] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def scan(self) -> list[str]:
        """
        Scan data/fonts/ and load all world.json files into the cache.

        Called once at startup.  Returns the list of discovered world names.
        """
        self._fonts_dir.mkdir(parents=True, exist_ok=True)
        discovered: list[str] = []

        for entry in sorted(self._fonts_dir.iterdir()):
            if not entry.is_dir():
                continue
            world_name = entry.name
            schema = self._load_from_disk(world_name)
            self._cache[world_name] = schema
            discovered.append(world_name)
            logger.debug(
                "WorldRegistry: discovered '%s' (%s)",
                world_name, schema.narrative_tone or "no tone defined",
            )

        logger.info(
            "WorldRegistry scan complete: %d world(s) found — %s",
            len(discovered),
            ", ".join(discovered) or "none",
        )
        return discovered

    # ── Discovery / Lookup ────────────────────────────────────────────────────

    def list_worlds(self) -> list[WorldSchema]:
        """Return all cached world schemas, sorted by display_name."""
        return sorted(self._cache.values(), key=lambda s: s.display_name.lower())

    def get_schema(self, world_name: str) -> WorldSchema | None:
        """Return the cached schema for a world, or None if unknown."""
        return self._cache.get(world_name)

    def reload(self, world_name: str) -> WorldSchema:
        """Force a re-read of world.json from disk and update the cache."""
        schema = self._load_from_disk(world_name)
        self._cache[world_name] = schema
        logger.info("WorldRegistry: reloaded schema for '%s'", world_name)
        return schema

    # ── Manifest (Unsealed Command) ───────────────────────────────────────────

    async def manifest(self, world_name: str) -> tuple[WorldSchema, bool]:
        """
        Ensure a world exists on disk, creating it if necessary.

        Returns (schema, manifested) where `manifested` is True only when
        the world folder was newly created by this call.

        Called by /switch_world when the target world does not yet exist.
        """
        if world_name in self._cache:
            # Already known — just register it with RealityWall to create
            # handouts/echo_vault silos if they don't exist yet.
            await self._reality_wall.register_world(world_name)
            return self._cache[world_name], False

        world_dir = self._fonts_dir / world_name
        manifested = not world_dir.exists()

        if manifested:
            world_dir.mkdir(parents=True, exist_ok=True)
            # Write a minimal world.json the user can expand later
            minimal: dict = {
                "display_name":   _slugify(world_name),
                "primary_color":  "#FFFFFF",
                "narrative_tone": "",
                "description":    (
                    f"This is {_slugify(world_name)}. "
                    "Edit data/fonts/"
                    f"{world_name}/world.json to define the tone and description."
                ),
                "system": world_name,
            }
            json_path = world_dir / _WORLD_JSON
            json_path.write_text(json.dumps(minimal, indent=2), encoding="utf-8")
            logger.info(
                "WorldRegistry: manifested new world '%s' at %s", world_name, world_dir
            )

        schema = self._load_from_disk(world_name)
        self._cache[world_name] = schema
        await self._reality_wall.register_world(world_name)
        return schema, manifested

    # ── Campaign-Scoped Helpers ───────────────────────────────────────────────

    async def switch_campaign_world(
        self, campaign_id: str, world_name: str
    ) -> tuple[WorldSchema, bool]:
        """
        Manifest the world (if needed), bind the campaign to it in
        RealityWall, and return (schema, manifested).
        """
        schema, manifested = await self.manifest(world_name)
        await self._reality_wall.set_current_world(campaign_id, world_name)
        return schema, manifested

    async def get_campaign_schema(self, campaign_id: str) -> WorldSchema | None:
        """Return the WorldSchema for whatever world the campaign is currently in."""
        world_name = await self._reality_wall.get_current_world(campaign_id)
        if world_name is None:
            return None
        # Auto-load if cache miss (e.g. after a restart)
        if world_name not in self._cache:
            self.reload(world_name)
        return self._cache.get(world_name)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_from_disk(self, world_name: str) -> WorldSchema:
        """Load world.json for `world_name`, returning a minimal schema if absent."""
        json_path = self._fonts_dir / world_name / _WORLD_JSON
        if json_path.exists():
            try:
                raw = json.loads(json_path.read_text(encoding="utf-8"))
                # Ensure system defaults to folder name if blank
                if not raw.get("system"):
                    raw["system"] = world_name
                return WorldSchema(**raw)
            except Exception as exc:
                logger.warning(
                    "WorldRegistry: failed to parse world.json for '%s': %s — "
                    "using minimal schema.",
                    world_name, exc,
                )

        return WorldSchema(
            display_name=_slugify(world_name),
            system=world_name,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    """Convert folder names like 'vampire_the_masquerade' → 'Vampire The Masquerade'."""
    return " ".join(word.capitalize() for word in name.replace("-", "_").split("_"))

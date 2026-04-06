"""
Fog of War Service — Redis-backed spatial state store
======================================================
Manages the active Fog-of-War bitmask and player coordinate map for each
campaign. The hot path lives entirely in Redis so every read/write is
sub-millisecond; durable persistence is handled separately by PostgreSQL
(map_state / map_entities tables via migration 014).

Redis key schema
----------------
  fow:<campaign_id>                – JSON list of revealed cell indices
  map:pos:<campaign_id>:<pid>      – JSON {x, y, token} player position
  map:png:<campaign_id>            – raw PNG binary rendered by map-renderer

Public API
----------
  update_position()   – move a player/NPC token and auto-reveal nearby cells
  reveal_cells()      – reveal an explicit list of cell indices
  reset_map()         – clear all FoW data for a campaign
  get_revealed()      – fetch current revealed cell set
  get_positions()     – fetch all entity positions for a campaign
  get_map_png_url()   – return the map-renderer HTTP URL for this campaign
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from orchestrator.config import Settings
from orchestrator.services.cache import CacheService
from orchestrator.services.nats_bus import NATSBus

logger = logging.getLogger(__name__)


class FogOfWarService:
    """Redis-backed Fog-of-War and player-coordinate manager."""

    def __init__(
        self,
        cache: CacheService,
        nats_bus: NATSBus,
        settings: Settings,
    ) -> None:
        self._cache    = cache
        self._nats     = nats_bus
        self._settings = settings

    # ── Position & FoW updates ────────────────────────────────────────────────

    async def update_position(
        self,
        campaign_id: str,
        entity_id: str,
        x: int,
        y: int,
        token: str = "",
        reveal_radius: int = 3,
        cols: int = 20,
        rows: int = 20,
    ) -> None:
        """
        Move an entity to (x, y) and reveal nearby cells within *reveal_radius*.
        Publishes a NATS `map.update.*` event so the map-renderer re-renders.
        """
        # Clamp to grid
        cx = max(0, min(cols - 1, int(round(x))))
        cy = max(0, min(rows - 1, int(round(y))))

        await self._cache.client.set(
            f"map:pos:{campaign_id}:{entity_id}",
            json.dumps({"x": cx, "y": cy, "token": token}),
        )

        # Reveal circle around new position
        revealed = await self._load_revealed(campaign_id)
        for dy in range(-reveal_radius, reveal_radius + 1):
            for dx in range(-reveal_radius, reveal_radius + 1):
                if dx * dx + dy * dy > reveal_radius * reveal_radius:
                    continue
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < cols and 0 <= ny < rows:
                    revealed.add(ny * cols + nx)

        await self._save_revealed(campaign_id, revealed)

        # Notify map-renderer via NATS
        await self._nats.publish_coordinate_update(
            campaign_id, entity_id, cx, cy, token, reveal_radius
        )

    async def reveal_cells(self, campaign_id: str, cells: list[int]) -> None:
        """Reveal an explicit list of flat grid-cell indices."""
        revealed = await self._load_revealed(campaign_id)
        revealed.update(cells)
        await self._save_revealed(campaign_id, revealed)
        await self._nats.publish_fog_reveal(campaign_id, cells)

    async def reset_map(self, campaign_id: str) -> None:
        """Clear all Fog-of-War and entity positions for a campaign."""
        await self._cache.client.delete(f"fow:{campaign_id}")
        pos_keys = await self._cache.client.keys(f"map:pos:{campaign_id}:*")
        if pos_keys:
            await self._cache.client.delete(*pos_keys)
        await self._cache.client.delete(f"map:png:{campaign_id}")
        await self._nats.publish_map_reset(campaign_id)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_revealed(self, campaign_id: str) -> list[int]:
        """Return the sorted list of revealed cell indices."""
        return sorted(await self._load_revealed(campaign_id))

    async def get_positions(self, campaign_id: str) -> dict[str, dict[str, Any]]:
        """Return a mapping of entity_id → {x, y, token} for a campaign."""
        keys = await self._cache.client.keys(f"map:pos:{campaign_id}:*")
        positions: dict[str, dict[str, Any]] = {}
        for key in keys:
            raw = await self._cache.client.get(key)
            if not raw:
                continue
            entity_id = key.split(":")[-1]
            try:
                positions[entity_id] = json.loads(raw)
            except json.JSONDecodeError:
                pass
        return positions

    def get_map_png_url(self, campaign_id: str) -> str:
        """Return the map-renderer HTTP URL that serves the campaign PNG."""
        return f"{self._settings.map_renderer_url}/map/{campaign_id}"

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _load_revealed(self, campaign_id: str) -> set[int]:
        raw = await self._cache.client.get(f"fow:{campaign_id}")
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return set()

    async def _save_revealed(self, campaign_id: str, revealed: set[int]) -> None:
        await self._cache.client.set(
            f"fow:{campaign_id}", json.dumps(sorted(revealed))
        )

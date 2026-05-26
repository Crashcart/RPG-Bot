"""
Ironclad GM — World Object Tracker
====================================
Persistent memory for in-game objects: weapons, containers, locations, props.

Solves the problem of LLMs "forgetting" established visual/textual continuity
(e.g. what the cursed sword looked like three sessions ago, or what was inside
the ornate chest the party found) by maintaining a canonical per-UUID record in
PostgreSQL that any future prompt can query.

Design decisions
----------------
- ``base_description`` is immutable — written once at registration, never overwritten.
  The LLM is always anchored to this ground-truth description.
- ``current_state`` (JSONB) is freely mutable via shallow-merge patch operations.
- Objects form a parent-child tree via ``parent_entity_id`` for containers
  (backpack → coins, scroll case → scrolls, etc.).
- Terminal statuses (``destroyed``, ``consumed``) raise ``ObjectMutationError``;
  ``locked`` status also blocks mutations until explicitly unlocked.
- ``get_context_summary()`` returns a single token-efficient string ready for
  direct injection into an LLM system prompt without bloating the context window.

Usage example
-------------
    tracker = ObjectTracker(settings, pool)

    # Register a new world object
    entity_id = await tracker.register_object(
        campaign_id="...",
        base_description="A worn leather backpack, patched with copper rivets.",
        image_url="media://handouts/backpack_001.png",
    )

    # Add child items
    potion_id = await tracker.register_object(
        campaign_id="...",
        base_description="A cloudy healing potion in a cracked vial.",
        parent_entity_id=entity_id,
        initial_state={"charges": 1},
    )

    # Update dynamic state
    await tracker.mutate_state(entity_id, {"contents_count": 3})

    # Consume an item
    await tracker.set_status(potion_id, WorldObjectStatus.CONSUMED)

    # Generate LLM-ready context
    summary = await tracker.get_context_summary(entity_id)
    # → "A worn leather backpack, patched with copper rivets. (img:backpack_001.png)
    #    [active] — contents_count=3"
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from orchestrator.config import Settings
from orchestrator.schemas.object_tracker_schemas import (
    WorldObjectRecord,
    WorldObjectStatus,
)

logger = logging.getLogger(__name__)


class ObjectMutationError(Exception):
    """Raised when a mutation is rejected due to object status constraints."""


class ObjectTracker:
    def __init__(self, settings: Settings, pool) -> None:
        self._pool = pool

    # ── Registration ──────────────────────────────────────────────────────────

    async def register_object(
        self,
        campaign_id: str,
        base_description: str,
        image_url: str = "",
        parent_entity_id: str | None = None,
        initial_state: dict | None = None,
    ) -> str:
        """
        Create a new world object and return its entity_id (UUID string).
        ``base_description`` is the immutable ground-truth; it cannot be changed later.
        """
        row = await self._pool.fetchrow(
            """
            INSERT INTO world_objects
                (campaign_id, base_description, image_url,
                 current_state, parent_entity_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING entity_id
            """,
            UUID(campaign_id),
            base_description,
            image_url,
            json.dumps(initial_state or {}),
            UUID(parent_entity_id) if parent_entity_id else None,
        )
        entity_id = str(row["entity_id"])
        logger.info(
            "Object registered: entity_id=%s campaign=%s",
            entity_id, campaign_id,
        )
        return entity_id

    # ── State Mutation ────────────────────────────────────────────────────────

    async def mutate_state(
        self,
        entity_id: str,
        state_patch: dict,
        new_image_url: str | None = None,
    ) -> WorldObjectRecord:
        """
        Shallow-merge ``state_patch`` into ``current_state``.

        Raises
        ------
        ObjectMutationError
            If the object does not exist, or its status is ``destroyed``,
            ``consumed``, or ``locked``.

        Returns the updated ``WorldObjectRecord``.
        """
        row = await self._pool.fetchrow(
            "SELECT object_status, current_state FROM world_objects WHERE entity_id = $1",
            UUID(entity_id),
        )
        if row is None:
            raise ObjectMutationError(f"Object {entity_id} not found")

        status = row["object_status"]
        if status in ("destroyed", "consumed"):
            raise ObjectMutationError(
                f"Object {entity_id} is {status} — no further mutations allowed"
            )
        if status == "locked":
            raise ObjectMutationError(
                f"Object {entity_id} is locked — call set_status(ACTIVE) first"
            )

        existing: dict = row["current_state"] or {}
        merged = {**existing, **state_patch}

        if new_image_url is not None:
            updated = await self._pool.fetchrow(
                """
                UPDATE world_objects
                SET current_state = $1, image_url = $3
                WHERE entity_id = $2
                RETURNING entity_id, campaign_id, base_description, image_url,
                          current_state, parent_entity_id, object_status,
                          created_at, updated_at
                """,
                json.dumps(merged),
                UUID(entity_id),
                new_image_url,
            )
        else:
            updated = await self._pool.fetchrow(
                """
                UPDATE world_objects
                SET current_state = $1
                WHERE entity_id = $2
                RETURNING entity_id, campaign_id, base_description, image_url,
                          current_state, parent_entity_id, object_status,
                          created_at, updated_at
                """,
                json.dumps(merged),
                UUID(entity_id),
            )

        logger.debug("Object mutated: entity_id=%s patch=%s", entity_id, list(state_patch))
        return _row_to_record(updated)

    async def set_status(
        self, entity_id: str, status: WorldObjectStatus
    ) -> WorldObjectRecord:
        """Transition the object's lifecycle status (e.g. active → locked)."""
        row = await self._pool.fetchrow(
            """
            UPDATE world_objects
            SET object_status = $1
            WHERE entity_id = $2
            RETURNING entity_id, campaign_id, base_description, image_url,
                      current_state, parent_entity_id, object_status,
                      created_at, updated_at
            """,
            status.value,
            UUID(entity_id),
        )
        if row is None:
            raise ObjectMutationError(f"Object {entity_id} not found")
        logger.info("Object %s status → %s", entity_id, status.value)
        return _row_to_record(row)

    # ── Retrieval ─────────────────────────────────────────────────────────────

    async def get_object(self, entity_id: str) -> WorldObjectRecord | None:
        """Fetch a single object by entity_id. Returns None if not found."""
        row = await self._pool.fetchrow(
            """
            SELECT entity_id, campaign_id, base_description, image_url,
                   current_state, parent_entity_id, object_status,
                   created_at, updated_at
            FROM world_objects WHERE entity_id = $1
            """,
            UUID(entity_id),
        )
        return _row_to_record(row) if row else None

    async def get_children(
        self, parent_entity_id: str
    ) -> list[WorldObjectRecord]:
        """Return all direct children of a container object, ordered by creation time."""
        rows = await self._pool.fetch(
            """
            SELECT entity_id, campaign_id, base_description, image_url,
                   current_state, parent_entity_id, object_status,
                   created_at, updated_at
            FROM world_objects
            WHERE parent_entity_id = $1
            ORDER BY created_at
            """,
            UUID(parent_entity_id),
        )
        return [_row_to_record(r) for r in rows]

    async def get_objects_for_campaign(
        self,
        campaign_id: str,
        status_filter: WorldObjectStatus | None = None,
        limit: int = 100,
    ) -> list[WorldObjectRecord]:
        """List objects in a campaign, optionally filtered by status."""
        if status_filter:
            rows = await self._pool.fetch(
                """
                SELECT entity_id, campaign_id, base_description, image_url,
                       current_state, parent_entity_id, object_status,
                       created_at, updated_at
                FROM world_objects
                WHERE campaign_id = $1 AND object_status = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                UUID(campaign_id),
                status_filter.value,
                limit,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT entity_id, campaign_id, base_description, image_url,
                       current_state, parent_entity_id, object_status,
                       created_at, updated_at
                FROM world_objects
                WHERE campaign_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                UUID(campaign_id),
                limit,
            )
        return [_row_to_record(r) for r in rows]

    # ── LLM Context Generation ────────────────────────────────────────────────

    async def get_context_summary(self, entity_id: str) -> str:
        """
        Produce a single token-efficient string for LLM prompt injection.

        Format::

            <base_description> (img:<filename>) [<status>] — <key>=<val>, ...

        Returns empty string if the object does not exist.
        """
        obj = await self.get_object(entity_id)
        if obj is None:
            return ""
        return _format_summary(obj)

    async def bulk_context_for_scene(
        self, entity_ids: list[str]
    ) -> str:
        """
        Produce a compact multi-line context block covering all listed objects.
        Safe to inject verbatim into an LLM system prompt; one line per object.
        """
        if not entity_ids:
            return ""

        uuid_list = [UUID(eid) for eid in entity_ids]
        rows = await self._pool.fetch(
            """
            SELECT entity_id, campaign_id, base_description, image_url,
                   current_state, parent_entity_id, object_status,
                   created_at, updated_at
            FROM world_objects
            WHERE entity_id = ANY($1::uuid[])
            ORDER BY created_at
            """,
            uuid_list,
        )
        if not rows:
            return ""
        return "\n".join(_format_summary(_row_to_record(r)) for r in rows)


# ── Module-level helpers ───────────────────────────────────────────────────────

def _row_to_record(row) -> WorldObjectRecord:
    return WorldObjectRecord(
        entity_id=str(row["entity_id"]),
        campaign_id=str(row["campaign_id"]),
        base_description=row["base_description"],
        image_url=row["image_url"] or "",
        current_state=row["current_state"] or {},
        parent_entity_id=(
            str(row["parent_entity_id"]) if row["parent_entity_id"] else None
        ),
        object_status=WorldObjectStatus(row["object_status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _format_summary(obj: WorldObjectRecord) -> str:
    """Compact one-line description with image reference and dynamic state."""
    img_ref = ""
    if obj.image_url:
        filename = obj.image_url.rstrip("/").split("/")[-1]
        img_ref = f" (img:{filename})"

    state_parts = [
        f"{k}={v}" for k, v in obj.current_state.items() if v is not None
    ]
    state_str = " — " + ", ".join(state_parts) if state_parts else ""

    return f"{obj.base_description}{img_ref} [{obj.object_status.value}]{state_str}"

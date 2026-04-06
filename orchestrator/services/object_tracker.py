"""
Ironclad GM – Object Tracker Service
======================================
Persistent Visual & Textual Object State Tracker (TDR §3).

Provides a reliable, searchable registry of "what things look like" and
"what things hold" across long RPG sessions.  Every in-game object (item,
container, artefact, location, NPC prop …) is assigned a UUID at registration.
State, image reference, and inventory contents are tracked with full
audit history.

Key guarantees:
  • base_description is immutable after registration (DB trigger + service guard).
  • Objects in 'locked' or 'destroyed' state reject contents mutations.
  • 'destroyed' is a terminal state — it cannot be reversed.
  • Perceptual-hash deduplication prevents duplicate images for identical assets.
  • A summary compiler converts any object into a token-efficient LLM string.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.schemas.payloads import (
    ContentsOperationType,
    EntityContentsRequest,
    EntityObjectMutateRequest,
    EntityObjectRecord,
    EntityObjectRegisterRequest,
    EntityObjectState,
    EntityObjectSummary,
)

logger = logging.getLogger(__name__)


class ObjectTrackerService:
    """
    CRUD + business logic for the entity_objects table.

    Injected with a DatabaseService instance at startup; all DB access
    goes through db.execute() / db.fetch() / db.fetchrow() helpers.
    """

    def __init__(self, db) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Entity Registration
    # ------------------------------------------------------------------

    async def register(
        self, request: EntityObjectRegisterRequest
    ) -> EntityObjectRecord:
        """
        Register a new tracked entity and return its full record.

        If phash is provided and an entity with the same phash already
        exists in this campaign, the existing entity is returned instead
        (perceptual-hash deduplication — TDR Option 2).
        """
        # ── Perceptual-hash deduplication ─────────────────────────────
        if request.phash is not None:
            existing = await self._find_by_phash(request.campaign_id, request.phash)
            if existing:
                logger.debug(
                    "ObjectTracker: phash %d already registered as %s — deduplicating",
                    request.phash, existing["entity_id"],
                )
                return _row_to_record(existing)

        row = await self._db.fetchrow(
            """
            INSERT INTO entity_objects
                (campaign_id, display_name, entity_type,
                 base_description, image_url, phash,
                 owner_entity_id, extra_data)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            RETURNING *
            """,
            request.campaign_id,
            request.display_name,
            request.entity_type,
            request.base_description,
            request.image_url,
            request.phash,
            request.owner_entity_id,
            json.dumps(request.extra_data),
        )
        logger.info(
            "ObjectTracker: registered entity '%s' (%s) in campaign %s",
            request.display_name, row["entity_id"], request.campaign_id,
        )
        return _row_to_record(row)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def get(self, entity_id: str) -> EntityObjectRecord | None:
        """Fetch a single entity by UUID.  Returns None if not found."""
        row = await self._db.fetchrow(
            "SELECT * FROM entity_objects WHERE entity_id = $1", entity_id
        )
        return _row_to_record(row) if row else None

    async def list_by_campaign(
        self,
        campaign_id: str,
        entity_type: str | None = None,
        state: EntityObjectState | None = None,
        limit: int = 100,
    ) -> list[EntityObjectRecord]:
        """
        List entities for a campaign with optional type/state filters,
        ordered newest-first.
        """
        filters: list[str] = ["campaign_id = $1"]
        params:  list[Any] = [campaign_id]

        if entity_type:
            params.append(entity_type)
            filters.append(f"entity_type = ${len(params)}")
        if state:
            params.append(state.value)
            filters.append(f"current_state = ${len(params)}::entity_object_state")

        params.append(limit)
        where = " AND ".join(filters)
        rows = await self._db.fetch(
            f"SELECT * FROM entity_objects WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
        return [_row_to_record(r) for r in rows]

    async def get_children(self, entity_id: str) -> list[EntityObjectRecord]:
        """Return all entities directly owned by this entity."""
        rows = await self._db.fetch(
            "SELECT * FROM entity_objects WHERE owner_entity_id = $1 "
            "ORDER BY created_at ASC",
            entity_id,
        )
        return [_row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # State Mutation
    # ------------------------------------------------------------------

    async def mutate(
        self,
        entity_id: str,
        request:   EntityObjectMutateRequest,
    ) -> EntityObjectRecord:
        """
        Apply mutable field updates to an entity.

        Rules:
          • base_description cannot be changed (DB trigger enforces this).
          • 'destroyed' is terminal — cannot transition to any other state.
          • extra_data is *merged*, not replaced, so callers only need to
            send the keys that change.
        """
        row = await self._db.fetchrow(
            "SELECT * FROM entity_objects WHERE entity_id = $1", entity_id
        )
        if not row:
            raise KeyError(f"Entity {entity_id} not found")

        current = EntityObjectState(row["current_state"])

        # Terminal-state guard
        if current == EntityObjectState.DESTROYED:
            raise ValueError(
                f"Entity {entity_id} is destroyed and cannot be mutated."
            )

        # Build the SET clause dynamically based on non-None fields
        updates: dict[str, Any] = {}

        if request.new_state is not None:
            updates["current_state"] = request.new_state.value

        if request.image_url is not None:
            updates["image_url"] = request.image_url

        if request.phash is not None:
            updates["phash"] = request.phash

        if request.extra_data is not None:
            # Merge: read existing JSONB, overlay new keys
            existing_extra: dict = json.loads(row["extra_data"]) if row["extra_data"] else {}
            existing_extra.update(request.extra_data)
            updates["extra_data"] = json.dumps(existing_extra)

        if not updates:
            return _row_to_record(row)  # nothing to do

        # Build SET clause with explicit PG casts for typed columns
        set_parts = []
        cast_values: list[Any] = []
        for i, (col, val) in enumerate(updates.items(), start=2):
            if col == "current_state":
                set_parts.append(f"{col} = ${i}::entity_object_state")
            elif col == "extra_data":
                set_parts.append(f"{col} = ${i}::jsonb")
            else:
                set_parts.append(f"{col} = ${i}")
            cast_values.append(val)

        set_clause = ", ".join(set_parts)

        # Record audit history before committing the change
        prev_state = row["current_state"]
        new_state  = updates.get("current_state", prev_state)
        await self._record_history(
            entity_id       = entity_id,
            campaign_id     = str(row["campaign_id"]),
            changed_by      = request.changed_by,
            previous_state  = prev_state,
            new_state       = new_state,
            previous_inv    = row["inventory_array"],
            new_inv         = row["inventory_array"],
            change_note     = request.change_note,
        )

        updated = await self._db.fetchrow(
            f"UPDATE entity_objects SET {set_clause} "
            f"WHERE entity_id = $1 RETURNING *",
            entity_id, *cast_values,
        )
        logger.info(
            "ObjectTracker: mutated entity %s → state=%s", entity_id, new_state
        )
        return _row_to_record(updated)

    # ------------------------------------------------------------------
    # Contents (Inventory) Operations
    # ------------------------------------------------------------------

    async def update_contents(
        self,
        entity_id: str,
        request:   EntityContentsRequest,
    ) -> EntityObjectRecord:
        """
        Add, remove, or clear items in a container entity.

        Raises ValueError if the entity is locked or destroyed.
        Raises KeyError if the entity does not exist.
        """
        row = await self._db.fetchrow(
            "SELECT * FROM entity_objects WHERE entity_id = $1", entity_id
        )
        if not row:
            raise KeyError(f"Entity {entity_id} not found")

        state = EntityObjectState(row["current_state"])
        if state in (EntityObjectState.LOCKED, EntityObjectState.DESTROYED):
            raise ValueError(
                f"Cannot modify contents of entity {entity_id}: "
                f"current state is '{state.value}'."
            )

        inventory: list[Any] = json.loads(row["inventory_array"]) if row["inventory_array"] else []
        prev_inv = list(inventory)

        if request.operation == ContentsOperationType.ADD:
            if request.item is None:
                raise ValueError("'add' operation requires a non-null 'item' field")
            inventory.append(request.item)

        elif request.operation == ContentsOperationType.REMOVE:
            if request.item is None:
                raise ValueError("'remove' operation requires a non-null 'item' field")
            try:
                inventory.remove(request.item)
            except ValueError:
                raise ValueError(
                    f"Item {request.item!r} not found in entity {entity_id} inventory"
                )

        elif request.operation == ContentsOperationType.CLEAR:
            inventory = []

        await self._record_history(
            entity_id       = entity_id,
            campaign_id     = str(row["campaign_id"]),
            changed_by      = request.changed_by,
            previous_state  = row["current_state"],
            new_state       = row["current_state"],
            previous_inv    = json.dumps(prev_inv),
            new_inv         = json.dumps(inventory),
            change_note     = request.change_note or f"contents.{request.operation.value}",
        )

        updated = await self._db.fetchrow(
            "UPDATE entity_objects SET inventory_array = $2::jsonb "
            "WHERE entity_id = $1 RETURNING *",
            entity_id, json.dumps(inventory),
        )
        logger.debug(
            "ObjectTracker: %s contents of %s, new count=%d",
            request.operation.value, entity_id, len(inventory),
        )
        return _row_to_record(updated)

    # ------------------------------------------------------------------
    # Summary Compiler (TDR Option 3)
    # ------------------------------------------------------------------

    async def compile_summary(self, entity_id: str) -> EntityObjectSummary:
        """
        Compile a token-efficient summary string for LLM context injection.

        Format:
          "<display_name> [<state>] (img: <url>) — <base_description[:100]>
           Contents: <item1>, <item2>, …"

        If the entity has no image, the img reference is omitted.
        If the inventory is empty, 'Contents: empty' is appended.
        """
        row = await self._db.fetchrow(
            "SELECT * FROM entity_objects WHERE entity_id = $1", entity_id
        )
        if not row:
            raise KeyError(f"Entity {entity_id} not found")

        record = _row_to_record(row)

        # Build description snippet
        desc = record.base_description[:100]
        if len(record.base_description) > 100:
            desc += "…"

        # Build contents list
        inv = record.inventory_array
        if inv:
            # Resolve child names for UUID references where possible
            item_labels: list[str] = []
            for item in inv:
                if isinstance(item, str):
                    # Attempt to resolve UUID → display_name
                    child = await self._db.fetchrow(
                        "SELECT display_name FROM entity_objects WHERE entity_id = $1",
                        item,
                    )
                    item_labels.append(child["display_name"] if child else item)
                elif isinstance(item, dict):
                    name = item.get("name", str(item))
                    qty  = item.get("qty", item.get("quantity"))
                    item_labels.append(f"{qty}× {name}" if qty is not None else name)
                else:
                    item_labels.append(str(item))
            contents_str = ", ".join(item_labels)
        else:
            contents_str = "empty"

        img_part = f" (img: {record.image_url})" if record.image_url else ""
        summary  = (
            f"{record.display_name} [{record.current_state.value}]{img_part}"
        )
        if desc:
            summary += f" — {desc}"
        summary += f". Contents: {contents_str}."

        return EntityObjectSummary(
            entity_id     = entity_id,
            display_name  = record.display_name,
            summary_text  = summary,
            image_url     = record.image_url,
            current_state = record.current_state,
        )

    # ------------------------------------------------------------------
    # Deduplication helpers
    # ------------------------------------------------------------------

    async def find_by_phash(
        self, campaign_id: str, phash: int
    ) -> EntityObjectRecord | None:
        """Return an existing entity with this perceptual hash, or None."""
        row = await self._find_by_phash(campaign_id, phash)
        return _row_to_record(row) if row else None

    async def _find_by_phash(self, campaign_id: str, phash: int):
        return await self._db.fetchrow(
            "SELECT * FROM entity_objects WHERE campaign_id = $1 AND phash = $2",
            campaign_id, phash,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_history(
        self,
        entity_id:      str,
        campaign_id:    str,
        changed_by:     str,
        previous_state: str,
        new_state:      str,
        previous_inv:   Any,
        new_inv:        Any,
        change_note:    str,
    ) -> None:
        """Insert a row into entity_object_history for audit purposes."""
        prev_inv_str = (
            json.dumps(previous_inv) if not isinstance(previous_inv, str) else previous_inv
        )
        new_inv_str = (
            json.dumps(new_inv) if not isinstance(new_inv, str) else new_inv
        )
        try:
            await self._db.execute(
                """
                INSERT INTO entity_object_history
                    (entity_id, campaign_id, changed_by,
                     previous_state, new_state, previous_inv, new_inv, change_note)
                VALUES ($1, $2, $3,
                        $4::entity_object_state, $5::entity_object_state,
                        $6::jsonb, $7::jsonb, $8)
                """,
                entity_id, campaign_id, changed_by,
                previous_state, new_state,
                prev_inv_str, new_inv_str, change_note,
            )
        except Exception as exc:
            logger.warning("ObjectTracker: failed to write history row: %s", exc)


# ── Row → Pydantic model conversion ──────────────────────────────────────────

def _row_to_record(row) -> EntityObjectRecord:
    """Convert an asyncpg Record (or dict-like) to an EntityObjectRecord."""
    inv = row["inventory_array"]
    if isinstance(inv, str):
        inv = json.loads(inv)
    elif inv is None:
        inv = []

    extra = row["extra_data"]
    if isinstance(extra, str):
        extra = json.loads(extra)
    elif extra is None:
        extra = {}

    return EntityObjectRecord(
        entity_id        = str(row["entity_id"]),
        campaign_id      = str(row["campaign_id"]),
        display_name     = row["display_name"] or "",
        entity_type      = row["entity_type"] or "item",
        image_url        = row["image_url"] or "",
        phash            = row["phash"],
        base_description = row["base_description"] or "",
        current_state    = EntityObjectState(row["current_state"]),
        inventory_array  = inv if isinstance(inv, list) else [],
        owner_entity_id  = str(row["owner_entity_id"]) if row["owner_entity_id"] else None,
        extra_data       = extra if isinstance(extra, dict) else {},
        created_at       = row["created_at"],
        updated_at       = row["updated_at"],
    )

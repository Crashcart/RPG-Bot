"""
Tests for ObjectTracker — Persistent Visual & Textual Object State Tracker.
Issue #7.

All asyncpg pool calls are mocked via AsyncMock so no live database is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from orchestrator.schemas.object_tracker_schemas import (
    WorldObjectRecord,
    WorldObjectStatus,
)
from orchestrator.services.object_tracker import (
    ObjectMutationError,
    ObjectTracker,
    _format_summary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CAMPAIGN_ID = str(uuid4())
ENTITY_ID   = str(uuid4())
PARENT_ID   = str(uuid4())
_NOW        = datetime.now(timezone.utc)


def _make_row(
    entity_id: str = ENTITY_ID,
    campaign_id: str = CAMPAIGN_ID,
    base_description: str = "A cracked leather satchel.",
    image_url: str = "media://handouts/satchel.png",
    current_state: dict | None = None,
    parent_entity_id: str | None = None,
    object_status: str = "active",
) -> dict:
    """Build a dict that behaves like an asyncpg Record row."""
    return {
        "entity_id":        entity_id,
        "campaign_id":      campaign_id,
        "base_description": base_description,
        "image_url":        image_url,
        "current_state":    current_state or {},
        "parent_entity_id": parent_entity_id,
        "object_status":    object_status,
        "created_at":       _NOW,
        "updated_at":       _NOW,
    }


def _make_tracker() -> tuple[ObjectTracker, MagicMock]:
    pool = MagicMock()
    pool.fetchrow  = AsyncMock()
    pool.fetch     = AsyncMock()
    pool.execute   = AsyncMock()
    settings       = MagicMock()
    return ObjectTracker(settings, pool), pool


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_returns_entity_id(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = {"entity_id": ENTITY_ID}

        result = await tracker.register_object(
            campaign_id=CAMPAIGN_ID,
            base_description="A cracked leather satchel.",
        )

        assert result == ENTITY_ID
        pool.fetchrow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_register_with_parent_passes_parent_uuid(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = {"entity_id": ENTITY_ID}

        await tracker.register_object(
            campaign_id=CAMPAIGN_ID,
            base_description="A healing potion.",
            parent_entity_id=PARENT_ID,
            initial_state={"charges": 1},
        )

        call_args = pool.fetchrow.call_args[0]
        # $5 is parent_entity_id (5th positional arg after the SQL string)
        assert call_args[4] is not None

    @pytest.mark.asyncio
    async def test_register_without_parent_passes_none(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = {"entity_id": ENTITY_ID}

        await tracker.register_object(
            campaign_id=CAMPAIGN_ID,
            base_description="A standalone artifact.",
        )

        call_args = pool.fetchrow.call_args[0]
        assert call_args[4] is None


# ---------------------------------------------------------------------------
# Mutation tests
# ---------------------------------------------------------------------------

class TestMutateState:
    @pytest.mark.asyncio
    async def test_active_object_can_be_mutated(self):
        tracker, pool = _make_tracker()
        existing = _make_row(current_state={"hp": 10})
        updated  = _make_row(current_state={"hp": 10, "charges": 3})
        pool.fetchrow.side_effect = [existing, updated]

        record = await tracker.mutate_state(ENTITY_ID, {"charges": 3})

        assert record.current_state["charges"] == 3
        assert record.current_state["hp"] == 10

    @pytest.mark.asyncio
    async def test_destroyed_object_raises(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row(object_status="destroyed")

        with pytest.raises(ObjectMutationError, match="destroyed"):
            await tracker.mutate_state(ENTITY_ID, {"key": "val"})

    @pytest.mark.asyncio
    async def test_consumed_object_raises(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row(object_status="consumed")

        with pytest.raises(ObjectMutationError, match="consumed"):
            await tracker.mutate_state(ENTITY_ID, {"key": "val"})

    @pytest.mark.asyncio
    async def test_locked_object_raises(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row(object_status="locked")

        with pytest.raises(ObjectMutationError, match="locked"):
            await tracker.mutate_state(ENTITY_ID, {"key": "val"})

    @pytest.mark.asyncio
    async def test_missing_object_raises(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = None

        with pytest.raises(ObjectMutationError, match="not found"):
            await tracker.mutate_state(ENTITY_ID, {"key": "val"})

    @pytest.mark.asyncio
    async def test_mutate_with_new_image_url(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.side_effect = [
            _make_row(object_status="active", current_state={}),
            _make_row(image_url="media://new.png", current_state={"broken": True}),
        ]

        record = await tracker.mutate_state(
            ENTITY_ID, {"broken": True}, new_image_url="media://new.png"
        )

        assert record.image_url == "media://new.png"
        # Second fetchrow call (the UPDATE) must include the new image URL
        second_call = pool.fetchrow.call_args_list[1][0]
        assert "media://new.png" in second_call


# ---------------------------------------------------------------------------
# Status transition tests
# ---------------------------------------------------------------------------

class TestSetStatus:
    @pytest.mark.asyncio
    async def test_set_status_to_locked(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row(object_status="locked")

        record = await tracker.set_status(ENTITY_ID, WorldObjectStatus.LOCKED)

        assert record.object_status == WorldObjectStatus.LOCKED

    @pytest.mark.asyncio
    async def test_set_status_not_found_raises(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = None

        with pytest.raises(ObjectMutationError, match="not found"):
            await tracker.set_status(ENTITY_ID, WorldObjectStatus.DESTROYED)


# ---------------------------------------------------------------------------
# Retrieval tests
# ---------------------------------------------------------------------------

class TestRetrieval:
    @pytest.mark.asyncio
    async def test_get_object_returns_record(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row()

        record = await tracker.get_object(ENTITY_ID)

        assert record is not None
        assert record.entity_id == ENTITY_ID
        assert record.object_status == WorldObjectStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_get_object_returns_none_for_missing(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = None

        result = await tracker.get_object(ENTITY_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_children_returns_list(self):
        tracker, pool = _make_tracker()
        child_a = _make_row(entity_id=str(uuid4()), base_description="Gold coin.")
        child_b = _make_row(entity_id=str(uuid4()), base_description="Rusted key.")
        pool.fetch.return_value = [child_a, child_b]

        children = await tracker.get_children(PARENT_ID)

        assert len(children) == 2
        assert children[0].base_description == "Gold coin."
        assert children[1].base_description == "Rusted key."

    @pytest.mark.asyncio
    async def test_get_children_empty_container(self):
        tracker, pool = _make_tracker()
        pool.fetch.return_value = []

        children = await tracker.get_children(PARENT_ID)

        assert children == []


# ---------------------------------------------------------------------------
# Context summary tests
# ---------------------------------------------------------------------------

class TestContextSummary:
    @pytest.mark.asyncio
    async def test_summary_contains_description_and_status(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row(
            base_description="A cursed silver dagger.",
            image_url="",
            current_state={},
        )

        summary = await tracker.get_context_summary(ENTITY_ID)

        assert "A cursed silver dagger." in summary
        assert "[active]" in summary

    @pytest.mark.asyncio
    async def test_summary_includes_image_filename(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row(
            image_url="media://handouts/dagger_cursed.png",
            current_state={},
        )

        summary = await tracker.get_context_summary(ENTITY_ID)

        assert "(img:dagger_cursed.png)" in summary

    @pytest.mark.asyncio
    async def test_summary_includes_state_kvs(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = _make_row(
            current_state={"charges": 3, "cursed": True},
        )

        summary = await tracker.get_context_summary(ENTITY_ID)

        assert "charges=3" in summary
        assert "cursed=True" in summary

    @pytest.mark.asyncio
    async def test_summary_empty_for_missing_object(self):
        tracker, pool = _make_tracker()
        pool.fetchrow.return_value = None

        summary = await tracker.get_context_summary(ENTITY_ID)

        assert summary == ""

    @pytest.mark.asyncio
    async def test_bulk_context_multi_line(self):
        tracker, pool = _make_tracker()
        rows = [
            _make_row(base_description="Shield.", current_state={}),
            _make_row(base_description="Helmet.", current_state={"dented": True}),
        ]
        pool.fetch.return_value = rows

        result = await tracker.bulk_context_for_scene(
            [str(uuid4()), str(uuid4())]
        )

        lines = result.splitlines()
        assert len(lines) == 2
        assert "Shield." in lines[0]
        assert "Helmet." in lines[1]

    @pytest.mark.asyncio
    async def test_bulk_context_empty_list(self):
        tracker, pool = _make_tracker()

        result = await tracker.bulk_context_for_scene([])

        assert result == ""
        pool.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# _format_summary unit tests
# ---------------------------------------------------------------------------

class TestFormatSummary:
    def _record(self, **kwargs) -> WorldObjectRecord:
        defaults = dict(
            entity_id=ENTITY_ID,
            campaign_id=CAMPAIGN_ID,
            base_description="Test object.",
            image_url="",
            current_state={},
            parent_entity_id=None,
            object_status=WorldObjectStatus.ACTIVE,
            created_at=_NOW,
            updated_at=_NOW,
        )
        defaults.update(kwargs)
        return WorldObjectRecord(**defaults)

    def test_no_image_no_state(self):
        r = self._record()
        assert _format_summary(r) == "Test object. [active]"

    def test_with_image_basename_only(self):
        r = self._record(image_url="http://media-proxy:8001/handouts/sword.png")
        assert "(img:sword.png)" in _format_summary(r)

    def test_destroyed_status_in_output(self):
        r = self._record(object_status=WorldObjectStatus.DESTROYED)
        assert "[destroyed]" in _format_summary(r)

    def test_state_kv_pairs_appended(self):
        r = self._record(current_state={"hp": 5, "enchanted": False})
        result = _format_summary(r)
        assert "hp=5" in result
        assert "enchanted=False" in result

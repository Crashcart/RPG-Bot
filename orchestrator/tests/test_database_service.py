"""
Unit tests for orchestrator/services/database.py — DatabaseService.

All asyncpg interactions are fully mocked; no live database is required.
Run with:  pytest orchestrator/tests/test_database_service.py -v
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.config import Settings
from orchestrator.schemas.payloads import (
    CharacterStatus,
    StateDelta,
    StatDelta,
    SubsystemDelta,
    VehicleDelta,
)
from orchestrator.services.database import DatabaseService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_UUID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
CAMPAIGN_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _settings() -> Settings:
    return Settings(
        postgres_password="test",
        redis_password="test",
        gemini_api_key="test",
    )


def _fake_dt(formatted: str = "2026-08-07 10:00") -> MagicMock:
    """Return a mock that behaves like a datetime for .strftime() calls."""
    dt = MagicMock()
    dt.strftime.return_value = formatted
    return dt


def _make_pool() -> MagicMock:
    """Return a mock asyncpg Pool with async fetch methods."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    pool.fetch = AsyncMock()
    pool.execute = AsyncMock()
    pool.close = AsyncMock()
    return pool


def _make_conn(pool: MagicMock) -> AsyncMock:
    """
    Wire an async-context-manager acquire() + transaction() onto *pool*
    and return the underlying connection mock.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.execute = AsyncMock()

    txn_ctx = AsyncMock()
    txn_ctx.__aenter__ = AsyncMock(return_value=None)
    txn_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_ctx)

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)

    return conn


@pytest.fixture
def db() -> DatabaseService:
    svc = DatabaseService(_settings())
    svc._pool = _make_pool()
    return svc


# ---------------------------------------------------------------------------
# TestLifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_creates_pool(self):
        svc = DatabaseService(_settings())
        mock_pool = _make_pool()
        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            await svc.connect()
        assert svc._pool is mock_pool

    @pytest.mark.asyncio
    async def test_disconnect_closes_pool(self, db):
        await db.disconnect()
        db._pool.close.assert_awaited_once()

    def test_pool_property_raises_when_not_connected(self):
        svc = DatabaseService(_settings())
        with pytest.raises(RuntimeError, match="not connected"):
            _ = svc.pool

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_pool_is_none(self):
        svc = DatabaseService(_settings())
        await svc.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# TestGetCharacterByPlayer
# ---------------------------------------------------------------------------

class TestGetCharacterByPlayer:
    @pytest.mark.asyncio
    async def test_returns_character_snapshot(self, db):
        row = {
            "id": FAKE_UUID,
            "name": "Zara",
            "system": "mothership",
            "status": "ALIVE",
            "stats": json.dumps({"hp": 10}),
        }
        db._pool.fetchrow = AsyncMock(return_value=row)
        result = await db.get_character_by_player("player1", str(CAMPAIGN_UUID))
        assert result is not None
        assert result.name == "Zara"
        assert result.status == CharacterStatus.ALIVE
        assert result.stats == {"hp": 10}

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, db):
        db._pool.fetchrow = AsyncMock(return_value=None)
        result = await db.get_character_by_player("ghost", str(CAMPAIGN_UUID))
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_dict_stats_from_asyncpg(self, db):
        """asyncpg returns JSONB columns as dicts, not strings."""
        row = {
            "id": FAKE_UUID,
            "name": "Kira",
            "system": "shadowrun",
            "status": "ALIVE",
            "stats": {"hp": 8, "armor": 3},
        }
        db._pool.fetchrow = AsyncMock(return_value=row)
        result = await db.get_character_by_player("p2", str(CAMPAIGN_UUID))
        assert result.stats == {"hp": 8, "armor": 3}


# ---------------------------------------------------------------------------
# TestGetCharacterById
# ---------------------------------------------------------------------------

class TestGetCharacterById:
    @pytest.mark.asyncio
    async def test_returns_snapshot(self, db):
        row = {
            "id": FAKE_UUID,
            "name": "Brax",
            "system": "dnd5e",
            "status": "DEAD",
            "stats": json.dumps({"hp": 0}),
        }
        db._pool.fetchrow = AsyncMock(return_value=row)
        result = await db.get_character_by_id(str(FAKE_UUID))
        assert result.status == CharacterStatus.DEAD

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db):
        db._pool.fetchrow = AsyncMock(return_value=None)
        assert await db.get_character_by_id(str(FAKE_UUID)) is None


# ---------------------------------------------------------------------------
# TestGetInventory
# ---------------------------------------------------------------------------

class TestGetInventory:
    @pytest.mark.asyncio
    async def test_returns_item_list(self, db):
        rows = [
            {"item_data": json.dumps({"name": "Knife", "quantity": 1})},
            {"item_data": {"name": "Torch", "quantity": 3}},
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_inventory(str(FAKE_UUID))
        assert len(result) == 2
        assert result[0]["name"] == "Knife"
        assert result[1]["name"] == "Torch"

    @pytest.mark.asyncio
    async def test_returns_empty_list(self, db):
        db._pool.fetch = AsyncMock(return_value=[])
        assert await db.get_inventory(str(FAKE_UUID)) == []


# ---------------------------------------------------------------------------
# TestGetActiveCampaign
# ---------------------------------------------------------------------------

class TestGetActiveCampaign:
    @pytest.mark.asyncio
    async def test_returns_campaign_dict(self, db):
        row = {
            "id": CAMPAIGN_UUID,
            "name": "The Void Run",
            "system": "mothership",
            "settings": json.dumps({"difficulty": "hard"}),
        }
        db._pool.fetchrow = AsyncMock(return_value=row)
        result = await db.get_active_campaign("guild-1")
        assert result is not None
        assert result["name"] == "The Void Run"
        assert result["settings"] == {"difficulty": "hard"}

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active(self, db):
        db._pool.fetchrow = AsyncMock(return_value=None)
        assert await db.get_active_campaign("guild-x") is None


# ---------------------------------------------------------------------------
# TestApplyStateDelta
# ---------------------------------------------------------------------------

class TestApplyStateDelta:
    def _make_delta(self, **kwargs) -> StateDelta:
        defaults = {
            "character_id": str(FAKE_UUID),
            "stat_deltas": [],
            "status_change": None,
            "inventory_delta": [],
            "vehicle_deltas": [],
        }
        defaults.update(kwargs)
        return StateDelta(**defaults)

    @pytest.mark.asyncio
    async def test_applies_stat_changes(self, db):
        conn = _make_conn(db._pool)
        conn.fetchrow = AsyncMock(return_value={"stats": json.dumps({"hp": 10})})

        delta = self._make_delta(
            stat_deltas=[StatDelta(stat_key="hp", old_value=10, new_value=7)]
        )
        result = await db.apply_state_delta(delta)
        assert result["hp"] == 7
        conn.execute.assert_awaited()

    @pytest.mark.asyncio
    async def test_applies_status_change(self, db):
        conn = _make_conn(db._pool)
        conn.fetchrow = AsyncMock(return_value={"stats": json.dumps({"hp": 0})})

        delta = self._make_delta(
            stat_deltas=[StatDelta(stat_key="hp", old_value=3, new_value=0)],
            status_change=CharacterStatus.DEAD,
        )
        await db.apply_state_delta(delta)
        # The UPDATE call should include the status
        calls = conn.execute.await_args_list
        assert any("status" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_adds_new_inventory_item(self, db):
        conn = _make_conn(db._pool)
        conn.fetchrow = AsyncMock(side_effect=[
            {"stats": json.dumps({})},  # character fetch
            None,                        # no existing inventory item
        ])
        delta = self._make_delta(
            inventory_delta=[{"name": "Bandage", "quantity": 2}]
        )
        await db.apply_state_delta(delta)
        # INSERT into inventories should have been called
        assert any("INSERT" in str(c) for c in conn.execute.await_args_list)

    @pytest.mark.asyncio
    async def test_upserts_existing_inventory_item(self, db):
        existing_row = {
            "id": FAKE_UUID,
            "item_data": json.dumps({"name": "Bandage", "quantity": 1}),
        }
        conn = _make_conn(db._pool)
        conn.fetchrow = AsyncMock(side_effect=[
            {"stats": json.dumps({})},  # character fetch
            existing_row,               # existing inventory item found
        ])
        delta = self._make_delta(
            inventory_delta=[{"name": "Bandage", "quantity": 3}]
        )
        await db.apply_state_delta(delta)
        # UPDATE inventories should be called (not INSERT)
        assert any("UPDATE inventories" in str(c) for c in conn.execute.await_args_list)

    @pytest.mark.asyncio
    async def test_removes_inventory_item(self, db):
        conn = _make_conn(db._pool)
        conn.fetchrow = AsyncMock(return_value={"stats": json.dumps({})})
        delta = self._make_delta(
            inventory_delta=[{"name": "Torch", "quantity": -1}]
        )
        await db.apply_state_delta(delta)
        assert any("DELETE" in str(c) for c in conn.execute.await_args_list)

    @pytest.mark.asyncio
    async def test_raises_when_character_not_found(self, db):
        conn = _make_conn(db._pool)
        conn.fetchrow = AsyncMock(return_value=None)
        delta = self._make_delta()
        with pytest.raises(ValueError, match="not found"):
            await db.apply_state_delta(delta)


# ---------------------------------------------------------------------------
# TestLogAction
# ---------------------------------------------------------------------------

class TestLogAction:
    @pytest.mark.asyncio
    async def test_inserts_record(self, db):
        db._pool.execute = AsyncMock()
        record: dict[str, Any] = {
            "intent_id": str(FAKE_UUID),
            "campaign_id": str(CAMPAIGN_UUID),
            "character_id": str(FAKE_UUID),
            "player_id": "player1",
            "raw_input": "I attack",
            "intent_payload": {"type": "combat"},
            "mechanical_payload": {"outcome": "success"},
            "state_delta": {},
            "narrative_summary": "You hit the goblin.",
        }
        await db.log_action(record)
        db._pool.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestWebUI
# ---------------------------------------------------------------------------

class TestWebUI:
    @pytest.mark.asyncio
    async def test_get_all_campaigns(self, db):
        rows = [
            {
                "id": CAMPAIGN_UUID,
                "name": "Dark Run",
                "system": "mothership",
                "guild_id": "g1",
                "character_count": 3,
                "fact_count": 12,
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_all_campaigns()
        assert len(result) == 1
        assert result[0]["name"] == "Dark Run"

    @pytest.mark.asyncio
    async def test_get_dashboard_stats(self, db):
        row = {
            "campaigns": 2,
            "living": 5,
            "dead": 1,
            "rule_modules": 3,
            "story_facts": 44,
            "actions_today": 10,
        }
        db._pool.fetchrow = AsyncMock(return_value=row)
        result = await db.get_dashboard_stats()
        assert result["campaigns"] == 2
        assert result["living"] == 5

    @pytest.mark.asyncio
    async def test_get_recent_actions(self, db):
        dt = _fake_dt("08-07 10:00")
        rows = [
            {
                "player_id": "p1",
                "raw_input": "I flee",
                "narrative_summary": "You run.",
                "resolved_at": dt,
                "outcome": "success",
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_recent_actions(limit=1)
        assert result[0]["player_id"] == "p1"
        assert result[0]["resolved_at"] == "08-07 10:00"

    @pytest.mark.asyncio
    async def test_get_recent_actions_null_resolved_at(self, db):
        rows = [
            {
                "player_id": "p2",
                "raw_input": "look",
                "narrative_summary": None,
                "resolved_at": None,
                "outcome": None,
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_recent_actions()
        assert result[0]["resolved_at"] == ""
        assert result[0]["narrative_summary"] == ""


# ---------------------------------------------------------------------------
# TestRuleModules
# ---------------------------------------------------------------------------

class TestRuleModules:
    @pytest.mark.asyncio
    async def test_get_all_rule_modules(self, db):
        dt = _fake_dt("2026-08-07 09:00")
        rows = [
            {
                "id": FAKE_UUID,
                "module_name": "Core Rules",
                "module_type": "core",
                "chroma_collection": "col_1",
                "module_data": json.dumps({"version": 1}),
                "active": True,
                "loaded_at": dt,
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_all_rule_modules(str(CAMPAIGN_UUID))
        assert result[0]["module_name"] == "Core Rules"
        assert result[0]["module_data"] == {"version": 1}

    @pytest.mark.asyncio
    async def test_add_rule_module(self, db):
        db._pool.execute = AsyncMock()
        await db.add_rule_module(
            campaign_id=str(CAMPAIGN_UUID),
            module_name="Bestiary",
            module_type="monsters",
            module_data={"count": 50},
            chroma_collection="col_beast",
        )
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_toggle_rule_module(self, db):
        db._pool.execute = AsyncMock()
        await db.toggle_rule_module(str(FAKE_UUID))
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_rule_module(self, db):
        db._pool.execute = AsyncMock()
        await db.delete_rule_module(str(FAKE_UUID))
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_active_rule_modules(self, db):
        rows = [
            {
                "module_name": "Core",
                "module_type": "core",
                "chroma_collection": "col_core",
                "module_data": {"dice": "d6"},
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_active_rule_modules(str(CAMPAIGN_UUID))
        assert len(result) == 1
        assert result[0]["module_name"] == "Core"


# ---------------------------------------------------------------------------
# TestStoryContext
# ---------------------------------------------------------------------------

class TestStoryContext:
    @pytest.mark.asyncio
    async def test_get_story_context_unfiltered(self, db):
        dt = _fake_dt("2026-08-07 10:00")
        rows = [
            {
                "entity_type": "npc",
                "entity_name": "Grib",
                "summary": "A goblin merchant.",
                "detail": "Sells stolen goods.",
                "last_updated_at": dt,
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_story_context(str(CAMPAIGN_UUID))
        assert result[0]["entity_name"] == "Grib"

    @pytest.mark.asyncio
    async def test_get_story_context_filtered_by_type(self, db):
        db._pool.fetch = AsyncMock(return_value=[])
        result = await db.get_story_context(str(CAMPAIGN_UUID), entity_type="location")
        db._pool.fetch.assert_awaited_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_upsert_story_fact(self, db):
        db._pool.execute = AsyncMock()
        await db.upsert_story_fact(
            str(CAMPAIGN_UUID), "npc", "Grib", "A goblin.", "Sells stolen goods."
        )
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_story_fact(self, db):
        db._pool.execute = AsyncMock()
        await db.delete_story_fact(str(CAMPAIGN_UUID), "npc", "Grib")
        db._pool.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestActionLog
# ---------------------------------------------------------------------------

class TestActionLog:
    @pytest.mark.asyncio
    async def test_get_action_log_unfiltered(self, db):
        dt = _fake_dt("2026-08-07 10:00:00")
        rows = [
            {
                "player_id": "p1",
                "raw_input": "attack",
                "narrative_summary": "You strike!",
                "resolved_at": dt,
                "outcome": "success",
                "roll_result": "18",
                "difficulty": "12",
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_action_log(str(CAMPAIGN_UUID))
        assert result[0]["outcome"] == "success"
        assert result[0]["roll_result"] == "18"

    @pytest.mark.asyncio
    async def test_get_action_log_with_outcome_filter(self, db):
        db._pool.fetch = AsyncMock(return_value=[])
        result = await db.get_action_log(str(CAMPAIGN_UUID), outcome_filter="failure")
        db._pool.fetch.assert_awaited_once()
        assert result == []


# ---------------------------------------------------------------------------
# TestVehicles
# ---------------------------------------------------------------------------

class TestVehicles:
    @pytest.mark.asyncio
    async def test_get_vehicles_for_campaign(self, db):
        vehicle_row = {
            "id": FAKE_UUID,
            "name": "The Pale Horse",
            "asset_type": "starship",
            "hull_integrity": 80,
            "max_hull_integrity": 100,
            "asset_data": json.dumps({"class": "freighter"}),
        }
        subsystem_row = {
            "id": FAKE_UUID,
            "subsystem_name": "Engine",
            "subsystem_type": "propulsion",
            "operational_status": "OPERATIONAL",
            "assigned_character_id": None,
            "subsystem_data": json.dumps({"thrust": 5}),
        }
        db._pool.fetch = AsyncMock(side_effect=[
            [vehicle_row],
            [subsystem_row],
        ])
        result = await db.get_vehicles_for_campaign(str(CAMPAIGN_UUID))
        assert len(result) == 1
        assert result[0]["name"] == "The Pale Horse"
        assert result[0]["subsystems"][0]["subsystem_name"] == "Engine"

    @pytest.mark.asyncio
    async def test_get_vehicles_for_campaign_empty(self, db):
        db._pool.fetch = AsyncMock(return_value=[])
        result = await db.get_vehicles_for_campaign(str(CAMPAIGN_UUID))
        assert result == []

    @pytest.mark.asyncio
    async def test_apply_vehicle_delta_hull_change(self, db):
        conn = AsyncMock()
        conn.execute = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={
            "name": "Horse",
            "asset_type": "starship",
            "hull_integrity": 70,
            "max_hull_integrity": 100,
        })
        result = await db.apply_vehicle_delta(conn, str(FAKE_UUID), hull_delta=-10, subsystem_changes=[])
        conn.execute.assert_awaited()
        assert result["hull_integrity"] == 70

    @pytest.mark.asyncio
    async def test_apply_vehicle_delta_subsystem_status(self, db):
        conn = AsyncMock()
        conn.execute = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={
            "name": "Horse",
            "asset_type": "starship",
            "hull_integrity": 100,
            "max_hull_integrity": 100,
        })
        changes = [{"subsystem_name": "Engine", "new_status": "DAMAGED", "assigned_character_id": "__no_change__"}]
        await db.apply_vehicle_delta(conn, str(FAKE_UUID), hull_delta=0, subsystem_changes=changes)
        assert any("UPDATE vehicle_subsystems" in str(c) for c in conn.execute.await_args_list)


# ---------------------------------------------------------------------------
# TestNodeRegistry
# ---------------------------------------------------------------------------

class TestNodeRegistry:
    @pytest.mark.asyncio
    async def test_get_all_nodes(self, db):
        dt = _fake_dt("2026-08-07 10:00")
        rows = [
            {
                "id": FAKE_UUID,
                "node_name": "brain-01",
                "node_type": "ollama",
                "host": "http://brain:11434",
                "model": "mistral:7b",
                "priority": 1,
                "enabled": True,
                "status": "ok",
                "last_seen": dt,
                "notes": "",
                "roles": json.dumps(["gm", "adjudication"]),
                "latency_ms": 42,
                "latency_measured_at": dt,
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_all_nodes()
        assert result[0]["node_name"] == "brain-01"
        assert "gm" in result[0]["roles"]

    @pytest.mark.asyncio
    async def test_get_enabled_ollama_nodes(self, db):
        rows = [
            {
                "id": FAKE_UUID,
                "node_name": "brain-01",
                "host": "http://brain:11434",
                "model": "mistral:7b",
                "priority": 1,
                "status": "ok",
                "roles": ["gm"],
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_enabled_ollama_nodes()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_nodes_for_role(self, db):
        rows = [
            {
                "id": FAKE_UUID,
                "node_name": "brain-01",
                "host": "http://brain:11434",
                "model": "mistral:7b",
                "priority": 1,
                "status": "ok",
                "roles": ["gm"],
                "latency_ms": 55,
                "voice_id": None,
            }
        ]
        db._pool.fetch = AsyncMock(return_value=rows)
        result = await db.get_nodes_for_role("gm")
        assert result[0]["voice_id"] == "en-US-GuyNeural"  # default fallback

    @pytest.mark.asyncio
    async def test_get_nodes_for_role_by_latency(self, db):
        db._pool.fetch = AsyncMock(return_value=[])
        result = await db.get_nodes_for_role_by_latency("adjudication")
        assert result == []

    @pytest.mark.asyncio
    async def test_update_node_latency(self, db):
        db._pool.execute = AsyncMock()
        await db.update_node_latency("brain-01", 123)
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_node(self, db):
        db._pool.execute = AsyncMock()
        await db.upsert_node("brain-02", "ollama", "http://b:11434", "mistral:7b", 2, roles=["gm"])
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_node_status(self, db):
        db._pool.execute = AsyncMock()
        dt = _fake_dt()
        await db.update_node_status("brain-01", "ok", dt)
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_toggle_node(self, db):
        db._pool.execute = AsyncMock()
        await db.toggle_node(str(FAKE_UUID))
        db._pool.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_node(self, db):
        db._pool.execute = AsyncMock()
        await db.delete_node(str(FAKE_UUID))
        db._pool.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestSystemSettings
# ---------------------------------------------------------------------------

class TestSystemSettings:
    @pytest.mark.asyncio
    async def test_get_setting_returns_parsed_value(self, db):
        db._pool.fetchrow = AsyncMock(return_value={"value": json.dumps({"theme": "dark"})})
        result = await db.get_system_setting("ui_theme")
        assert result == {"theme": "dark"}

    @pytest.mark.asyncio
    async def test_get_setting_returns_default_when_missing(self, db):
        db._pool.fetchrow = AsyncMock(return_value=None)
        result = await db.get_system_setting("missing_key", default="fallback")
        assert result == "fallback"

    @pytest.mark.asyncio
    async def test_get_setting_handles_native_asyncpg_jsonb(self, db):
        """asyncpg can return JSONB as a native Python dict (no JSON string)."""
        db._pool.fetchrow = AsyncMock(return_value={"value": {"native": True}})
        result = await db.get_system_setting("flag")
        assert result == {"native": True}

    @pytest.mark.asyncio
    async def test_set_setting_upserts(self, db):
        db._pool.execute = AsyncMock()
        await db.set_system_setting("cloud_provider", "claude")
        db._pool.execute.assert_awaited_once()

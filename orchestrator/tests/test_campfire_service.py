"""
Unit tests for orchestrator/services/campfire.py

Covers:
  CampfireService.update_presence
  CampfireService.get_status
  CampfireService.is_campfire_active
  CampfireService.force_campfire_on / force_campfire_off
  CampfireService._recalculate_campfire  (via update_presence)
  CampfireService._read_setting / _write_setting  (via public API)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from orchestrator.services.campfire import CampfireService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(
    *,
    campaign_row=None,
    char_rows=None,
    presence_rows=None,
    setting_row=None,
):
    """Return an asyncpg pool mock wired for the common query pattern."""
    pool = MagicMock()

    # fetchrow: first call = campaign lookup; subsequent = _read_setting
    fetchrow_responses = []
    if campaign_row is not None:
        fetchrow_responses.append(campaign_row)

    async def fetchrow_side(*args, **kwargs):
        sql = args[0]
        if "campaigns" in sql:
            return campaign_row
        # _read_setting query
        if "system_settings" in sql:
            return setting_row
        return None

    pool.fetchrow = AsyncMock(side_effect=fetchrow_side)

    # fetch: first call = characters; second = player_presence
    fetch_call_count = [0]

    async def fetch_side(*args, **kwargs):
        fetch_call_count[0] += 1
        if fetch_call_count[0] == 1:
            return char_rows or []
        return presence_rows or []

    pool.fetch = AsyncMock(side_effect=fetch_side)
    pool.execute = AsyncMock()
    return pool


def _row(data: dict):
    """Minimal dict-like row mock."""
    return data


# ---------------------------------------------------------------------------
# TestUpdatePresence
# ---------------------------------------------------------------------------

class TestUpdatePresence:
    @pytest.mark.asyncio
    async def test_all_key_players_online_campfire_inactive(self):
        pool = _make_pool(
            campaign_row={"id": "camp-1"},
            char_rows=[{"player_id": "p1"}, {"player_id": "p2"}],
            presence_rows=[{"player_id": "p1", "online": True}, {"player_id": "p2", "online": True}],
        )
        svc = CampfireService(settings=MagicMock(), pool=pool)
        status = await svc.update_presence("p1", "guild-1", True)

        assert status.active is False
        assert status.absent_players == []

    @pytest.mark.asyncio
    async def test_one_player_offline_campfire_active(self):
        pool = _make_pool(
            campaign_row={"id": "camp-1"},
            char_rows=[{"player_id": "p1"}, {"player_id": "p2"}],
            presence_rows=[{"player_id": "p1", "online": True}],  # p2 has no row → offline
        )
        svc = CampfireService(settings=MagicMock(), pool=pool)
        status = await svc.update_presence("p1", "guild-1", True)

        assert status.active is True
        assert "p2" in status.absent_players

    @pytest.mark.asyncio
    async def test_no_active_campaign_returns_inactive(self):
        pool = _make_pool(campaign_row=None)
        svc = CampfireService(settings=MagicMock(), pool=pool)
        status = await svc.update_presence("p1", "guild-1", False)

        assert status.active is False
        assert status.guild_id == "guild-1"

    @pytest.mark.asyncio
    async def test_no_alive_characters_returns_inactive(self):
        pool = _make_pool(
            campaign_row={"id": "camp-1"},
            char_rows=[],  # no characters
        )
        svc = CampfireService(settings=MagicMock(), pool=pool)
        status = await svc.update_presence("p1", "guild-1", True)

        assert status.active is False

    @pytest.mark.asyncio
    async def test_player_with_no_presence_row_treated_as_offline(self):
        pool = _make_pool(
            campaign_row={"id": "camp-1"},
            char_rows=[{"player_id": "ghost"}],
            presence_rows=[],  # no presence row for "ghost"
        )
        svc = CampfireService(settings=MagicMock(), pool=pool)
        status = await svc.update_presence("ghost", "guild-1", False)

        assert status.active is True
        assert "ghost" in status.absent_players

    @pytest.mark.asyncio
    async def test_upserts_presence_row(self):
        pool = _make_pool(
            campaign_row={"id": "camp-1"},
            char_rows=[{"player_id": "p1"}],
            presence_rows=[{"player_id": "p1", "online": True}],
        )
        svc = CampfireService(settings=MagicMock(), pool=pool)
        await svc.update_presence("p1", "guild-1", True)

        # First execute call must be the INSERT...ON CONFLICT upsert
        first_call_sql = pool.execute.call_args_list[0][0][0]
        assert "player_presence" in first_call_sql
        assert "ON CONFLICT" in first_call_sql

    @pytest.mark.asyncio
    async def test_writes_campfire_settings_to_db(self):
        pool = _make_pool(
            campaign_row={"id": "camp-1"},
            char_rows=[{"player_id": "p1"}],
            presence_rows=[],  # p1 offline
        )
        svc = CampfireService(settings=MagicMock(), pool=pool)
        await svc.update_presence("p1", "guild-1", False)

        # At least two writes: campfire_mode_active + campfire_absent_players
        system_setting_calls = [
            c for c in pool.execute.call_args_list
            if "system_settings" in c[0][0]
        ]
        assert len(system_setting_calls) >= 2


# ---------------------------------------------------------------------------
# TestGetStatus
# ---------------------------------------------------------------------------

class TestGetStatus:
    @pytest.mark.asyncio
    async def test_returns_active_status_from_settings(self):
        pool = MagicMock()
        call_count = [0]

        async def fetchrow_side(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"value": json.dumps(True)}
            return {"value": json.dumps(["p99"])}

        pool.fetchrow = AsyncMock(side_effect=fetchrow_side)
        svc = CampfireService(settings=MagicMock(), pool=pool)
        status = await svc.get_status("guild-1")

        assert status.active is True
        assert "p99" in status.absent_players

    @pytest.mark.asyncio
    async def test_defaults_when_no_setting_rows(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)
        svc = CampfireService(settings=MagicMock(), pool=pool)
        status = await svc.get_status("guild-2")

        assert status.active is False
        assert status.absent_players == []
        assert status.guild_id == "guild-2"


# ---------------------------------------------------------------------------
# TestIsCampfireActive
# ---------------------------------------------------------------------------

class TestIsCampfireActive:
    @pytest.mark.asyncio
    async def test_returns_true_when_active(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={"value": json.dumps(True)})
        svc = CampfireService(settings=MagicMock(), pool=pool)
        assert await svc.is_campfire_active("guild-1") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_inactive(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={"value": json.dumps(False)})
        svc = CampfireService(settings=MagicMock(), pool=pool)
        assert await svc.is_campfire_active("guild-1") is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_row(self):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)
        svc = CampfireService(settings=MagicMock(), pool=pool)
        assert await svc.is_campfire_active("guild-1") is False


# ---------------------------------------------------------------------------
# TestManualControls
# ---------------------------------------------------------------------------

class TestManualControls:
    @pytest.mark.asyncio
    async def test_force_campfire_on_writes_true(self):
        pool = MagicMock()
        pool.execute = AsyncMock()
        svc = CampfireService(settings=MagicMock(), pool=pool)
        await svc.force_campfire_on("guild-1", reason="Test override")

        written_values = [
            json.loads(c[0][2])  # 3rd arg to execute is the JSON value
            for c in pool.execute.call_args_list
        ]
        assert True in written_values

    @pytest.mark.asyncio
    async def test_force_campfire_off_clears_absent_list(self):
        pool = MagicMock()
        pool.execute = AsyncMock()
        svc = CampfireService(settings=MagicMock(), pool=pool)
        await svc.force_campfire_off("guild-1")

        written_values = [
            json.loads(c[0][2])
            for c in pool.execute.call_args_list
        ]
        # False written for mode_active, [] written for absent_players
        assert False in written_values
        assert [] in written_values

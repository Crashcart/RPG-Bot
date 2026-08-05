"""
Unit tests for DowntimeService and CampfireService.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from orchestrator.schemas.payloads import (
    CampfireStatus,
    DowntimeSubmitRequest,
    DowntimeTaskStatus,
)
from orchestrator.services.campfire import CampfireService
from orchestrator.services.downtime import DowntimeService


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_settings(**kwargs):
    s = MagicMock()
    s.gemini_api_key = kwargs.get("gemini_api_key", "test-key")
    s.gemini_model = kwargs.get("gemini_model", "gemini-1.5-pro")
    s.ollama_host = kwargs.get("ollama_host", "http://brain:11434")
    s.ollama_model = kwargs.get("ollama_model", "mistral:7b-instruct")
    return s


def _make_pool():
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    pool.fetch = AsyncMock()
    pool.execute = AsyncMock()
    pool.executemany = AsyncMock()
    return pool


CAMPAIGN_ID = "a0000000-0000-0000-0000-000000000001"
PLAYER_ID = "123456789012345678"


# ─────────────────────────────────────────────────────────────────────────────
# DowntimeService tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDowntimeSubmitTask:
    """submit_task() persists a new downtime task."""

    @pytest.mark.asyncio
    async def test_submit_creates_row(self):
        pool = _make_pool()
        now = datetime.now(timezone.utc)
        pool.fetchrow.side_effect = [
            None,  # character lookup → no character
            {
                "id": UUID("b0000000-0000-0000-0000-000000000001"),
                "status": "pending",
                "submitted_at": now,
                "resolves_at": now,
            },
        ]
        svc = DowntimeService(_make_settings(), pool)
        req = DowntimeSubmitRequest(
            player_id=PLAYER_ID,
            guild_id="g1",
            campaign_id=CAMPAIGN_ID,
            description="Research the artifact",
            duration_hours=8,
        )
        result = await svc.submit_task(req)

        assert isinstance(result, DowntimeTaskStatus)
        assert result.status == "pending"
        assert result.description == "Research the artifact"
        assert result.duration_hours == 8

    @pytest.mark.asyncio
    async def test_submit_resolves_character_id(self):
        pool = _make_pool()
        now = datetime.now(timezone.utc)
        char_id = UUID("c0000000-0000-0000-0000-000000000002")
        pool.fetchrow.side_effect = [
            {"id": char_id},
            {
                "id": UUID("b0000000-0000-0000-0000-000000000002"),
                "status": "pending",
                "submitted_at": now,
                "resolves_at": now,
            },
        ]
        svc = DowntimeService(_make_settings(), pool)
        req = DowntimeSubmitRequest(
            player_id=PLAYER_ID,
            guild_id="g1",
            campaign_id=CAMPAIGN_ID,
            description="Train combat",
            duration_hours=4,
        )
        await svc.submit_task(req)

        insert_call_args = pool.fetchrow.call_args_list[1]
        assert char_id in insert_call_args.args


class TestDowntimeResolvePending:
    """resolve_pending() processes overdue tasks."""

    @pytest.mark.asyncio
    async def test_no_pending_returns_zero(self):
        pool = _make_pool()
        pool.fetch.return_value = []
        svc = DowntimeService(_make_settings(), pool)
        assert await svc.resolve_pending() == 0

    @pytest.mark.asyncio
    async def test_resolves_and_marks_complete(self):
        pool = _make_pool()
        task_id = UUID("d0000000-0000-0000-0000-000000000003")
        pool.fetch.return_value = [
            {
                "id": task_id,
                "player_id": PLAYER_ID,
                "character_id": None,
                "description": "Research",
                "duration_hours": 8,
                "campaign_id": UUID(CAMPAIGN_ID),
                "character_name": "Aria",
                "campaign_system": "D&D 5e",
            }
        ]
        svc = DowntimeService(_make_settings(), pool)

        with patch.object(svc, "_generate_narrative", AsyncMock(return_value="Great story!")):
            count = await svc.resolve_pending()

        assert count == 1
        calls = [str(c) for c in pool.execute.call_args_list]
        assert any("resolving" in c for c in calls)
        assert any("complete" in c for c in calls)

    @pytest.mark.asyncio
    async def test_marks_failed_on_narrative_error(self):
        pool = _make_pool()
        task_id = UUID("e0000000-0000-0000-0000-000000000004")
        pool.fetch.return_value = [
            {
                "id": task_id,
                "player_id": PLAYER_ID,
                "character_id": None,
                "description": "Fail",
                "duration_hours": 2,
                "campaign_id": UUID(CAMPAIGN_ID),
                "character_name": None,
                "campaign_system": None,
            }
        ]
        svc = DowntimeService(_make_settings(), pool)

        with patch.object(svc, "_generate_narrative", AsyncMock(side_effect=RuntimeError("boom"))):
            count = await svc.resolve_pending()

        assert count == 0
        calls = [str(c) for c in pool.execute.call_args_list]
        assert any("failed" in c for c in calls)


class TestDowntimeNotifications:
    """get_pending_notifications() and mark_notified()."""

    @pytest.mark.asyncio
    async def test_returns_unnotified_completions(self):
        pool = _make_pool()
        pool.fetch.return_value = [
            {
                "id": UUID("f0000000-0000-0000-0000-000000000005"),
                "result_narrative": "You discovered a secret passage.",
                "character_name": "Raven",
            }
        ]
        svc = DowntimeService(_make_settings(), pool)
        notes = await svc.get_pending_notifications(PLAYER_ID)

        assert len(notes) == 1
        assert notes[0].result_narrative == "You discovered a secret passage."
        assert notes[0].character_name == "Raven"
        assert notes[0].player_id == PLAYER_ID

    @pytest.mark.asyncio
    async def test_mark_notified_executes_update(self):
        pool = _make_pool()
        svc = DowntimeService(_make_settings(), pool)
        task_id = "f0000000-0000-0000-0000-000000000006"
        await svc.mark_notified(task_id)

        pool.execute.assert_called_once()
        sql, arg = pool.execute.call_args.args
        assert "notified" in sql.lower()
        assert arg == UUID(task_id)


class TestDowntimeNarrativeFallback:
    """_generate_narrative fallback chain: Gemini → Ollama → default."""

    @pytest.mark.asyncio
    async def test_gemini_success_returns_text(self):
        svc = DowntimeService(_make_settings(), _make_pool())
        with patch.object(svc, "_try_gemini", AsyncMock(return_value="Gemini story")):
            result = await svc._generate_narrative("explore", 4, "Hero", "Fantasy")
        assert result == "Gemini story"

    @pytest.mark.asyncio
    async def test_ollama_fallback_when_gemini_fails(self):
        svc = DowntimeService(_make_settings(), _make_pool())
        with patch.object(svc, "_try_gemini", AsyncMock(return_value=None)):
            with patch.object(svc, "_try_ollama", AsyncMock(return_value="Ollama story")):
                result = await svc._generate_narrative("fight", 2, "Warrior", "D&D")
        assert result == "Ollama story"

    @pytest.mark.asyncio
    async def test_default_fallback_when_both_fail(self):
        svc = DowntimeService(_make_settings(), _make_pool())
        with patch.object(svc, "_try_gemini", AsyncMock(return_value=None)):
            with patch.object(svc, "_try_ollama", AsyncMock(return_value=None)):
                result = await svc._generate_narrative("rest", 8, "Lira", "Mothership")
        assert "Lira" in result
        assert "8 hours" in result

    @pytest.mark.asyncio
    async def test_prompt_truncates_long_descriptions(self):
        svc = DowntimeService(_make_settings(), _make_pool())
        long_desc = "x" * 2000
        with patch.object(svc, "_try_gemini", AsyncMock(return_value="ok")):
            result = await svc._generate_narrative(long_desc, 1, "A", "B")
        assert result == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# CampfireService tests
# ─────────────────────────────────────────────────────────────────────────────


GUILD_ID = "guild_111"


class TestCampfireUpdatePresence:
    """update_presence() upserts and recalculates campfire state."""

    @pytest.mark.asyncio
    async def test_no_active_campaign_returns_inactive(self):
        pool = _make_pool()
        pool.fetchrow.return_value = None  # no active campaign
        svc = CampfireService(_make_settings(), pool)
        status = await svc.update_presence(PLAYER_ID, GUILD_ID, False)

        assert isinstance(status, CampfireStatus)
        assert status.active is False

    @pytest.mark.asyncio
    async def test_all_players_online_deactivates_campfire(self):
        pool = _make_pool()
        pool.fetchrow.return_value = {"id": UUID(CAMPAIGN_ID)}
        pool.fetch.side_effect = [
            [{"player_id": PLAYER_ID}],  # key_players
            [{"player_id": PLAYER_ID, "online": True}],  # presence
        ]
        pool.execute.return_value = None
        svc = CampfireService(_make_settings(), pool)

        with patch.object(svc, "_write_setting", AsyncMock()):
            with patch.object(svc, "_read_setting", AsyncMock(return_value=False)):
                status = await svc._recalculate_campfire(GUILD_ID)

        assert status.active is False

    @pytest.mark.asyncio
    async def test_offline_player_activates_campfire(self):
        pool = _make_pool()
        pool.fetchrow.return_value = {"id": UUID(CAMPAIGN_ID)}
        pool.fetch.side_effect = [
            [{"player_id": PLAYER_ID}],  # key_players
            [{"player_id": PLAYER_ID, "online": False}],  # presence → offline
        ]
        svc = CampfireService(_make_settings(), pool)

        with patch.object(svc, "_write_setting", AsyncMock()):
            status = await svc._recalculate_campfire(GUILD_ID)

        assert status.active is True
        assert PLAYER_ID in status.absent_players

    @pytest.mark.asyncio
    async def test_unknown_presence_treated_as_offline(self):
        pool = _make_pool()
        pool.fetchrow.return_value = {"id": UUID(CAMPAIGN_ID)}
        pool.fetch.side_effect = [
            [{"player_id": PLAYER_ID}],  # key_players
            [],  # no presence row → treated as offline
        ]
        svc = CampfireService(_make_settings(), pool)

        with patch.object(svc, "_write_setting", AsyncMock()):
            status = await svc._recalculate_campfire(GUILD_ID)

        assert status.active is True


class TestCampfireManualControls:
    """force_campfire_on / force_campfire_off."""

    @pytest.mark.asyncio
    async def test_force_on_writes_setting(self):
        svc = CampfireService(_make_settings(), _make_pool())
        with patch.object(svc, "_write_setting", AsyncMock()) as mock_write:
            await svc.force_campfire_on(GUILD_ID, reason="test")
        mock_write.assert_awaited_with("campfire_mode_active", True)

    @pytest.mark.asyncio
    async def test_force_off_clears_both_settings(self):
        svc = CampfireService(_make_settings(), _make_pool())
        calls: list[tuple] = []

        async def track(key, val):
            calls.append((key, val))

        with patch.object(svc, "_write_setting", track):
            await svc.force_campfire_off(GUILD_ID)

        keys = [c[0] for c in calls]
        assert "campfire_mode_active" in keys
        assert "campfire_absent_players" in keys


class TestCampfireStatusQuery:
    """get_status() and is_campfire_active()."""

    @pytest.mark.asyncio
    async def test_get_status_reflects_settings(self):
        svc = CampfireService(_make_settings(), _make_pool())

        async def fake_read(key, default):
            if key == "campfire_mode_active":
                return True
            if key == "campfire_absent_players":
                return [PLAYER_ID]
            return default

        with patch.object(svc, "_read_setting", fake_read):
            status = await svc.get_status(GUILD_ID)

        assert status.active is True
        assert PLAYER_ID in status.absent_players

    @pytest.mark.asyncio
    async def test_is_campfire_active_true_when_active(self):
        svc = CampfireService(_make_settings(), _make_pool())
        with patch.object(svc, "_read_setting", AsyncMock(return_value=True)):
            assert await svc.is_campfire_active(GUILD_ID) is True

    @pytest.mark.asyncio
    async def test_is_campfire_active_false_when_inactive(self):
        svc = CampfireService(_make_settings(), _make_pool())
        with patch.object(svc, "_read_setting", AsyncMock(return_value=False)):
            assert await svc.is_campfire_active(GUILD_ID) is False


class TestCampfireReadWriteSettings:
    """_read_setting() and _write_setting() internal helpers."""

    @pytest.mark.asyncio
    async def test_read_setting_returns_default_when_missing(self):
        pool = _make_pool()
        pool.fetchrow.return_value = None
        svc = CampfireService(_make_settings(), pool)
        val = await svc._read_setting("missing_key", "fallback")
        assert val == "fallback"

    @pytest.mark.asyncio
    async def test_read_setting_parses_json_string(self):
        pool = _make_pool()
        pool.fetchrow.return_value = {"value": json.dumps(["p1", "p2"])}
        svc = CampfireService(_make_settings(), pool)
        val = await svc._read_setting("campfire_absent_players", [])
        assert val == ["p1", "p2"]

    @pytest.mark.asyncio
    async def test_write_setting_upserts_json(self):
        pool = _make_pool()
        svc = CampfireService(_make_settings(), pool)
        await svc._write_setting("campfire_mode_active", True)
        pool.execute.assert_called_once()
        sql, key, val = pool.execute.call_args.args
        assert "campfire_mode_active" == key
        assert json.loads(val) is True

"""
Tests for ABES WorldTickService (orchestrator/services/abes_service.py).

All tests use MagicMock / AsyncMock to avoid a real DB connection.
Run with: pytest orchestrator/tests/test_abes.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest

from orchestrator.services.abes_service import WorldTickService, _roll


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

CAMPAIGN_ID = uuid4()


def _make_pool(fetchrow_return=None, fetch_return=None, execute_return=None):
    """Build a mock asyncpg pool that satisfies the service's DB calls."""
    pool = MagicMock()

    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.fetch    = AsyncMock(return_value=fetch_return or [])
    pool.execute  = AsyncMock(return_value=execute_return)

    # Context manager for pool.acquire() → conn
    conn = MagicMock()
    conn.execute      = AsyncMock()
    conn.transaction  = MagicMock(
        return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    pool.acquire = MagicMock(
        return_value=MagicMock(__aenter__=AsyncMock(return_value=conn),
                               __aexit__=AsyncMock())
    )
    pool._conn = conn  # expose for assertion
    return pool


def _make_intent(**overrides) -> dict[str, Any]:
    base = {
        "id":              uuid4(),
        "entity_name":     "Guard Captain Mord",
        "entity_type":     "npc",
        "intent_type":     "patrol",
        "description":     "Patrol the eastern watchtower road",
        "skill_rating":    12,
        "hp":              8,
        "max_hp":          10,
        "progress":        40,
        "destination":     "",
        "ticks_remaining": None,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Unit: _roll
# ─────────────────────────────────────────────────────────────────────────────

class TestRollHelper:
    def test_roll_within_range(self):
        for _ in range(200):
            result = _roll(20)
            assert 1 <= result <= 20

    def test_roll_d6_within_range(self):
        for _ in range(200):
            result = _roll(6)
            assert 1 <= result <= 6

    def test_roll_not_zero(self):
        # A d1 should always be 1
        assert _roll(1) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Unit: _process_npc_intent
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessNpcIntent:
    """Low-level tests for the intent-resolution logic."""

    @pytest.fixture
    def svc(self):
        pool = _make_pool()
        return WorldTickService(pool=pool)

    @pytest.mark.asyncio
    async def test_normal_progress(self, svc):
        intent = _make_intent(progress=50, skill_rating=15)
        # Force a high roll (natural success, no complication)
        with patch("orchestrator.services.abes_service._roll", side_effect=[15]):
            evt = await svc._process_npc_intent(CAMPAIGN_ID, intent, time_dilation=1.0)

        assert evt is not None
        assert evt["event_type"] == "progress"
        assert evt["severity"]   == "minor"
        assert evt["new_status"] == "active"
        assert "progress_after" in evt["mechanical_data"]

    @pytest.mark.asyncio
    async def test_complication_damages_hp(self, svc):
        intent = _make_intent(hp=8, max_hp=10, skill_rating=5)
        # roll=3 (≤ HAZARD_THRESHOLD=4, > CRITICAL_THRESHOLD=1)
        # second roll for damage: _roll(4)
        with patch("orchestrator.services.abes_service._roll", side_effect=[3, 2]):
            evt = await svc._process_npc_intent(CAMPAIGN_ID, intent, time_dilation=1.0)

        assert evt["event_type"] == "complication"
        assert evt["severity"]   == "minor"
        assert evt["mechanical_data"]["damage"] == 2
        assert evt["mechanical_data"]["hp_after"] == 6

    @pytest.mark.asyncio
    async def test_critical_failure_heavy_damage(self, svc):
        intent = _make_intent(hp=10, max_hp=10)
        # roll=1 (== CRITICAL_THRESHOLD), then _roll(6) for damage
        with patch("orchestrator.services.abes_service._roll", side_effect=[1, 5]):
            evt = await svc._process_npc_intent(CAMPAIGN_ID, intent, time_dilation=1.0)

        assert evt["severity"]   == "critical"
        assert evt["event_type"] == "complication"
        assert evt["mechanical_data"]["damage"] == 5 + 2  # _roll(6)+2

    @pytest.mark.asyncio
    async def test_death_when_hp_depleted(self, svc):
        intent = _make_intent(hp=2, max_hp=10)
        # roll=1 → critical, _roll(6)+2 = 8 → hp drops below 0
        with patch("orchestrator.services.abes_service._roll", side_effect=[1, 6]):
            evt = await svc._process_npc_intent(CAMPAIGN_ID, intent, time_dilation=1.0)

        assert evt["event_type"] == "death"
        assert evt["severity"]   == "critical"
        assert evt["new_status"] == "failed"

    @pytest.mark.asyncio
    async def test_task_completion_at_100_percent(self, svc):
        intent = _make_intent(progress=95)
        # roll=18 → high gain, should push progress over 100
        with patch("orchestrator.services.abes_service._roll", side_effect=[18]):
            evt = await svc._process_npc_intent(CAMPAIGN_ID, intent, time_dilation=1.0)

        assert evt["event_type"] == "task_complete"
        assert evt["severity"]   == "major"
        assert evt["new_status"] == "completed"

    @pytest.mark.asyncio
    async def test_time_dilation_accelerates_progress(self, svc):
        intent_slow = _make_intent(progress=0)
        intent_fast = _make_intent(progress=0)

        # Same roll for both
        with patch("orchestrator.services.abes_service._roll", return_value=10):
            evt_slow = await svc._process_npc_intent(CAMPAIGN_ID, intent_slow, time_dilation=1.0)
        with patch("orchestrator.services.abes_service._roll", return_value=10):
            evt_fast = await svc._process_npc_intent(CAMPAIGN_ID, intent_fast, time_dilation=3.0)

        slow_progress = evt_slow["mechanical_data"]["progress_after"]
        fast_progress = evt_fast["mechanical_data"]["progress_after"]
        assert fast_progress >= slow_progress

    @pytest.mark.asyncio
    async def test_ticks_remaining_countdown(self, svc):
        intent = _make_intent(ticks_remaining=1, progress=10)
        with patch("orchestrator.services.abes_service._roll", return_value=10):
            evt = await svc._process_npc_intent(CAMPAIGN_ID, intent, time_dilation=1.0)

        # With 1 tick left → reaches 0 → should fail
        assert evt["new_status"] == "failed"

    @pytest.mark.asyncio
    async def test_db_writes_are_called(self, svc):
        intent = _make_intent()
        with patch("orchestrator.services.abes_service._roll", return_value=15):
            await svc._process_npc_intent(CAMPAIGN_ID, intent, time_dilation=1.0)

        conn = svc._pool._conn
        assert conn.execute.call_count >= 2  # UPDATE npc_long_term_intents + INSERT world_delta


# ─────────────────────────────────────────────────────────────────────────────
# Unit: tick_campaign
# ─────────────────────────────────────────────────────────────────────────────

class TestTickCampaign:
    @pytest.fixture
    def svc(self):
        pool = _make_pool(fetch_return=[_make_intent(), _make_intent(entity_name="Spy Ring")])
        return WorldTickService(pool=pool)

    @pytest.mark.asyncio
    async def test_returns_one_event_per_intent(self, svc):
        with patch("orchestrator.services.abes_service._roll", return_value=12):
            events = await svc.tick_campaign(CAMPAIGN_ID)
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_empty_campaign_returns_no_events(self):
        pool = _make_pool(fetch_return=[])
        svc  = WorldTickService(pool=pool)
        events = await svc.tick_campaign(CAMPAIGN_ID)
        assert events == []


# ─────────────────────────────────────────────────────────────────────────────
# Unit: get_recent_events
# ─────────────────────────────────────────────────────────────────────────────

class TestGetRecentEvents:
    @pytest.fixture
    def svc(self):
        pool = _make_pool(fetch_return=[
            {
                "entity_name":     "Bandit Lord Kress",
                "event_type":      "task_complete",
                "severity":        "major",
                "summary":         "Bandit Lord Kress completed 'raid'.",
                "mechanical_data": {"roll": 19},
                "occurred_at":     datetime.now(timezone.utc),
            }
        ])
        return WorldTickService(pool=pool)

    @pytest.mark.asyncio
    async def test_returns_list(self, svc):
        events = await svc.get_recent_events(CAMPAIGN_ID)
        assert isinstance(events, list)
        assert len(events) == 1
        assert events[0]["entity_name"] == "Bandit Lord Kress"

    @pytest.mark.asyncio
    async def test_since_filter_is_applied(self, svc):
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        await svc.get_recent_events(CAMPAIGN_ID, since=since)
        # Verify the pool.fetch was called (since clause present)
        svc._pool.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_campaign_returns_empty(self):
        pool = _make_pool(fetch_return=[])
        svc  = WorldTickService(pool=pool)
        events = await svc.get_recent_events(CAMPAIGN_ID)
        assert events == []


# ─────────────────────────────────────────────────────────────────────────────
# Unit: upsert_npc_intent
# ─────────────────────────────────────────────────────────────────────────────

class TestUpsertNpcIntent:
    @pytest.fixture
    def svc(self):
        new_id = uuid4()
        pool = _make_pool(fetchrow_return={"id": new_id})
        return WorldTickService(pool=pool)

    @pytest.mark.asyncio
    async def test_returns_uuid_string(self, svc):
        result = await svc.upsert_npc_intent(
            campaign_id=CAMPAIGN_ID,
            entity_name="Merchant Guild",
            entity_type="faction",
            intent_type="trade",
            description="Establish trade route north",
            skill_rating=14,
            hp=20,
        )
        assert isinstance(result, str)
        # Should be parseable as UUID
        UUID(result)

    @pytest.mark.asyncio
    async def test_deactivates_previous_intent(self, svc):
        await svc.upsert_npc_intent(
            campaign_id=CAMPAIGN_ID,
            entity_name="Scout",
            entity_type="npc",
            intent_type="patrol",
            description="Patrol the border",
        )
        # First call should be the UPDATE (deactivate), second the INSERT
        svc._pool.execute.assert_called_once()  # UPDATE call
        svc._pool.fetchrow.assert_called_once()  # INSERT call


# ─────────────────────────────────────────────────────────────────────────────
# Unit: webhook
# ─────────────────────────────────────────────────────────────────────────────

class TestFireWebhook:
    @pytest.fixture
    def svc(self):
        return WorldTickService(pool=_make_pool())

    @pytest.mark.asyncio
    async def test_webhook_fires_on_major_event(self, svc):
        event = {
            "entity_name": "Dragon",
            "event_type":  "raid",
            "severity":    "major",
            "summary":     "The dragon completed a raid.",
            "mechanical_data": {},
        }
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__  = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock()))
            await svc._fire_webhook("https://discord.com/api/webhooks/test", event)
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_fails_silently(self, svc):
        event = {
            "entity_name": "Dragon",
            "event_type":  "raid",
            "severity":    "critical",
            "summary":     "Critical raid.",
            "mechanical_data": {},
        }
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__  = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("network error"))
            # Should not raise
            await svc._fire_webhook("https://discord.com/api/webhooks/test", event)

    @pytest.mark.asyncio
    async def test_no_webhook_skips_http(self, svc):
        event = {"severity": "major", "event_type": "raid",
                 "entity_name": "X", "summary": "Y", "mechanical_data": {}}
        with patch("httpx.AsyncClient") as mock_client_cls:
            await svc._fire_webhook("", event)
            mock_client_cls.assert_not_called()

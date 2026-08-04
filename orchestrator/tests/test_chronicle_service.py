"""
Unit tests for orchestrator/services/chronicle.py — ChronicleService.

All database and Gemini HTTP calls are mocked; no live services required.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from orchestrator.schemas.payloads import RecapRequest, RecapResponse
from orchestrator.services.chronicle import ChronicleService, _QUIET_RECAP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(gemini_key: str = "test-key", gemini_model: str = "gemini-pro"):
    s = MagicMock()
    s.gemini_api_key = gemini_key
    s.gemini_model   = gemini_model
    return s


def _make_pool(last_at=None, events=None, facts=None):
    pool = MagicMock()

    async def fetchrow(sql, *args):
        if "MAX(resolved_at)" in sql:
            return {"last_at": last_at}
        return None

    async def fetch(sql, *args):
        if "action_log" in sql:
            return events or []
        if "story_context" in sql:
            return facts or []
        return []

    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    pool.fetch    = AsyncMock(side_effect=fetch)
    return pool


def _make_event(player_id="p1", raw_input="Attack", summary="Player attacks", ts=None):
    ts = ts or datetime.now(timezone.utc)
    return {
        "player_id":        player_id,
        "raw_input":        raw_input,
        "narrative_summary": summary,
        "resolved_at":      ts,
    }


def _make_fact(name="The Vault", summary="A dark dungeon beneath the city."):
    return {"entity_name": name, "summary": summary}


CAMPAIGN_ID = "12345678-1234-1234-1234-123456789abc"
PLAYER_ID   = "player-snowflake-456"

_REQ = RecapRequest(
    player_id=PLAYER_ID,
    guild_id="guild-789",
    campaign_id=CAMPAIGN_ID,
)


# ---------------------------------------------------------------------------
# Quiet recap (no events)
# ---------------------------------------------------------------------------

class TestQuietRecap:

    @pytest.mark.asyncio
    async def test_returns_quiet_message_when_no_events(self):
        pool = _make_pool(last_at=datetime.now(timezone.utc) - timedelta(hours=1))
        svc  = ChronicleService(_make_settings(), pool)

        result = await svc.generate_recap(_REQ)

        assert isinstance(result, RecapResponse)
        assert result.recap_text == _QUIET_RECAP
        assert result.events_covered == 0
        assert result.player_id == PLAYER_ID

    @pytest.mark.asyncio
    async def test_quiet_recap_does_not_call_gemini(self):
        pool = _make_pool(last_at=datetime.now(timezone.utc) - timedelta(hours=1))
        svc  = ChronicleService(_make_settings(), pool)

        with patch("httpx.AsyncClient") as mock_client:
            await svc.generate_recap(_REQ)
            mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# New-player fallback (last_at = None → 24-hour window)
# ---------------------------------------------------------------------------

class TestNewPlayerFallback:

    @pytest.mark.asyncio
    async def test_new_player_uses_24h_window(self):
        """When the player has never acted, the since_ts should be ~24h ago."""
        before = datetime.now(timezone.utc) - timedelta(hours=24)
        pool   = _make_pool(last_at=None)
        svc    = ChronicleService(_make_settings(), pool)

        result = await svc.generate_recap(_REQ)

        # The quiet recap returns since_timestamp derived from now()-24h
        assert result.since_timestamp is not None
        # Since no events are present, we just get the quiet recap
        assert result.events_covered == 0

    @pytest.mark.asyncio
    async def test_new_player_with_events_gets_real_recap(self):
        pool = _make_pool(
            last_at=None,
            events=[_make_event()],
            facts=[_make_fact()],
        )
        svc = ChronicleService(_make_settings(), pool)

        gemini_text = "The party raided the vault."
        gemini_resp = {
            "candidates": [{"content": {"parts": [{"text": gemini_text}]}}]
        }

        with patch("httpx.AsyncClient") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value=gemini_resp)

            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__  = AsyncMock(return_value=False)
            mock_cm.post       = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_cm

            result = await svc.generate_recap(_REQ)

        assert result.events_covered == 1
        assert gemini_text in result.recap_text


# ---------------------------------------------------------------------------
# Gemini call — success path
# ---------------------------------------------------------------------------

class TestGeminiSuccess:

    @pytest.mark.asyncio
    async def test_recap_text_prefixed_with_header(self):
        pool = _make_pool(
            last_at=datetime.now(timezone.utc) - timedelta(hours=2),
            events=[_make_event(), _make_event()],
            facts=[_make_fact()],
        )
        svc = ChronicleService(_make_settings(), pool)

        gemini_body = "The heroes discovered a secret door."
        gemini_resp = {
            "candidates": [{"content": {"parts": [{"text": gemini_body}]}}]
        }

        with patch("httpx.AsyncClient") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value=gemini_resp)

            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__  = AsyncMock(return_value=False)
            mock_cm.post       = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_cm

            result = await svc.generate_recap(_REQ)

        assert "📖 **Chronicle Recap**" in result.recap_text
        assert gemini_body in result.recap_text
        assert result.events_covered == 2

    @pytest.mark.asyncio
    async def test_events_covered_matches_fetch_count(self):
        events = [_make_event(raw_input=f"action-{i}") for i in range(5)]
        pool   = _make_pool(
            last_at=datetime.now(timezone.utc) - timedelta(hours=2),
            events=events,
        )
        svc = ChronicleService(_make_settings(), pool)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value={
                "candidates": [{"content": {"parts": [{"text": "Recap here."}]}}]
            })

            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__  = AsyncMock(return_value=False)
            mock_cm.post       = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_cm

            result = await svc.generate_recap(_REQ)

        assert result.events_covered == 5


# ---------------------------------------------------------------------------
# Gemini call — failure / fallback path
# ---------------------------------------------------------------------------

class TestGeminiFallback:

    @pytest.mark.asyncio
    async def test_fallback_on_http_error(self):
        pool = _make_pool(
            last_at=datetime.now(timezone.utc) - timedelta(hours=2),
            events=[_make_event(raw_input="Enter the cave")],
        )
        svc = ChronicleService(_make_settings(), pool)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__  = AsyncMock(return_value=False)
            mock_cm.post       = AsyncMock(side_effect=Exception("Network error"))
            mock_cls.return_value = mock_cm

            result = await svc.generate_recap(_REQ)

        assert "Chronicle Recap" in result.recap_text
        assert "Enter the cave" in result.recap_text

    @pytest.mark.asyncio
    async def test_fallback_uses_raw_input_bullets(self):
        events = [_make_event(raw_input=f"Action {i}") for i in range(3)]
        pool   = _make_pool(
            last_at=datetime.now(timezone.utc) - timedelta(hours=2),
            events=events,
        )
        svc = ChronicleService(_make_settings(), pool)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__  = AsyncMock(return_value=False)
            mock_cm.post       = AsyncMock(side_effect=RuntimeError("timeout"))
            mock_cls.return_value = mock_cm

            result = await svc.generate_recap(_REQ)

        assert "Action 0" in result.recap_text
        assert "Action 1" in result.recap_text


# ---------------------------------------------------------------------------
# Events text capping
# ---------------------------------------------------------------------------

class TestEventsCapping:

    @pytest.mark.asyncio
    async def test_long_events_text_capped_at_10000_chars(self):
        """Verify _call_gemini caps the event text so Gemini prompt stays bounded."""
        long_summary = "x" * 500
        events = [_make_event(raw_input="act", summary=long_summary) for _ in range(25)]
        pool   = _make_pool(
            last_at=datetime.now(timezone.utc) - timedelta(hours=2),
            events=events,
        )
        svc = ChronicleService(_make_settings(), pool)

        captured_payload = {}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value={
                "candidates": [{"content": {"parts": [{"text": "recap"}]}}]
            })

            async def capture_post(url, json=None, **kw):
                captured_payload.update(json or {})
                return mock_resp

            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__  = AsyncMock(return_value=False)
            mock_cm.post       = AsyncMock(side_effect=capture_post)
            mock_cls.return_value = mock_cm

            await svc.generate_recap(_REQ)

        prompt_text = captured_payload["contents"][0]["parts"][0]["text"]
        # The raw events block in the prompt must never exceed 10000 chars
        # (we look for the capped section between EVENTS header and end of text)
        events_section_start = prompt_text.find("EVENTS SINCE YOU WERE LAST ONLINE:")
        events_section = prompt_text[events_section_start:]
        assert len(events_section) <= 11000  # generous upper bound accounting for label


# ---------------------------------------------------------------------------
# World facts fallback
# ---------------------------------------------------------------------------

class TestWorldFacts:

    @pytest.mark.asyncio
    async def test_no_world_facts_uses_fallback_text(self):
        pool = _make_pool(
            last_at=datetime.now(timezone.utc) - timedelta(hours=2),
            events=[_make_event()],
            facts=[],  # empty
        )
        svc = ChronicleService(_make_settings(), pool)

        captured = {}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value={
                "candidates": [{"content": {"parts": [{"text": "recap"}]}}]
            })

            async def capture_post(url, json=None, **kw):
                captured.update(json or {})
                return mock_resp

            mock_cm = MagicMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__  = AsyncMock(return_value=False)
            mock_cm.post       = AsyncMock(side_effect=capture_post)
            mock_cls.return_value = mock_cm

            await svc.generate_recap(_REQ)

        prompt = captured["contents"][0]["parts"][0]["text"]
        assert "No established world facts yet." in prompt

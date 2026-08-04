"""
Unit tests for orchestrator/services/handout_service.py — HandoutService.

All database and Gemini calls are mocked; no live services required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.handout_service import HandoutService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(**overrides):
    db = MagicMock()
    db.fetch    = AsyncMock(return_value=[])
    db.fetchrow = AsyncMock(return_value=None)
    db.execute  = AsyncMock()
    for k, v in overrides.items():
        setattr(db, k, v)
    return db


def _make_gemini(content: str = "Generated handout text"):
    g = MagicMock()
    g.generate = AsyncMock(return_value=content)
    return g


def _handout_row(handout_id="hid-1", title="Treasure Map", content="X marks the spot",
                 handout_type="map", is_global=False, creator="gm"):
    return {
        "id":           handout_id,
        "title":        title,
        "content_text": content,
        "image_url":    "",
        "handout_type": handout_type,
        "is_global":    is_global,
        "creator":      creator,
        "created_at":   None,
        "campaign_id":  "campaign-1",
    }


# ---------------------------------------------------------------------------
# AI authoring
# ---------------------------------------------------------------------------

class TestAiWriteHandout:

    @pytest.mark.asyncio
    async def test_returns_generated_content(self):
        db  = _make_db()
        g   = _make_gemini("A cryptic letter sealed with a wax emblem.")
        svc = HandoutService(db=db, gemini_client=g)

        content = await svc.ai_write_handout(
            campaign_id="c1",
            title="Mysterious Letter",
            handout_type="letter",
            brief="A noble's plea for help",
            tone="formal Elizabethan",
        )

        assert content == "A cryptic letter sealed with a wax emblem."
        g.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_prompt_contains_title_and_type(self):
        db  = _make_db()
        g   = _make_gemini()
        svc = HandoutService(db=db, gemini_client=g)

        await svc.ai_write_handout(
            campaign_id="c1",
            title="Wanted Poster",
            handout_type="poster",
        )

        call_kwargs = g.generate.call_args[1]
        assert "Wanted Poster" in call_kwargs["user"]
        assert "poster"        in call_kwargs["user"]

    @pytest.mark.asyncio
    async def test_fallback_message_on_gemini_error(self):
        db = _make_db()
        g  = MagicMock()
        g.generate = AsyncMock(side_effect=Exception("API unavailable"))
        svc = HandoutService(db=db, gemini_client=g)

        content = await svc.ai_write_handout("c1", "Forbidden Tome")

        assert "could not be generated" in content.lower() or "[Document" in content

    @pytest.mark.asyncio
    async def test_uses_default_brief_when_not_provided(self):
        db  = _make_db()
        g   = _make_gemini()
        svc = HandoutService(db=db, gemini_client=g)

        await svc.ai_write_handout("c1", "Notice Board")

        user_prompt = g.generate.call_args[1]["user"]
        assert "No additional context provided." in user_prompt

    @pytest.mark.asyncio
    async def test_strips_whitespace_from_output(self):
        db  = _make_db()
        g   = _make_gemini("  \n  trimmed content  \n  ")
        svc = HandoutService(db=db, gemini_client=g)

        content = await svc.ai_write_handout("c1", "Test")
        assert content == "trimmed content"


# ---------------------------------------------------------------------------
# create_handout / get_handout
# ---------------------------------------------------------------------------

class TestCrud:

    @pytest.mark.asyncio
    async def test_create_handout_returns_uuid_string(self):
        db  = _make_db()
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        handout_id = await svc.create_handout(
            campaign_id="c1",
            title="Elven Scroll",
            content_text="Ancient runes describe a lost city.",
        )

        assert isinstance(handout_id, str)
        assert len(handout_id) == 36  # UUID length

    @pytest.mark.asyncio
    async def test_create_handout_calls_db_insert(self):
        db  = _make_db()
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        await svc.create_handout("c1", "Map", "content")

        db.execute.assert_called_once()
        sql = db.execute.call_args[0][0]
        assert "INSERT INTO handouts" in sql

    @pytest.mark.asyncio
    async def test_get_handout_returns_dict_when_found(self):
        row = _handout_row()
        db  = _make_db(fetchrow=AsyncMock(return_value=row))
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        result = await svc.get_handout("hid-1")

        assert result is not None
        assert result["title"] == "Treasure Map"

    @pytest.mark.asyncio
    async def test_get_handout_returns_none_when_not_found(self):
        db  = _make_db(fetchrow=AsyncMock(return_value=None))
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        result = await svc.get_handout("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_campaign_handouts_returns_list(self):
        rows = [_handout_row(handout_id=f"hid-{i}") for i in range(3)]
        db   = _make_db(fetch=AsyncMock(return_value=rows))
        svc  = HandoutService(db=db, gemini_client=_make_gemini())

        result = await svc.list_campaign_handouts("c1")

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_campaign_handouts_passes_limit(self):
        db  = _make_db(fetch=AsyncMock(return_value=[]))
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        await svc.list_campaign_handouts("c1", limit=10)

        call_args = db.fetch.call_args[0]
        assert 10 in call_args


# ---------------------------------------------------------------------------
# deliver / get_player_handouts / get_pending_for_player
# ---------------------------------------------------------------------------

class TestDelivery:

    @pytest.mark.asyncio
    async def test_deliver_inserts_recipient(self):
        db  = _make_db()
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        await svc.deliver("handout-1", "player-1")

        db.execute.assert_called_once()
        sql = db.execute.call_args[0][0]
        assert "handout_recipients" in sql

    @pytest.mark.asyncio
    async def test_deliver_uses_on_conflict_do_nothing(self):
        db  = _make_db()
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        await svc.deliver("handout-1", "player-1")

        sql = db.execute.call_args[0][0]
        assert "ON CONFLICT" in sql.upper()

    @pytest.mark.asyncio
    async def test_deliver_is_safe_to_call_twice(self):
        """Two calls must not raise (idempotent via ON CONFLICT DO NOTHING)."""
        db  = _make_db()
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        await svc.deliver("h1", "p1")
        await svc.deliver("h1", "p1")
        assert db.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_get_player_handouts_joins_recipients(self):
        rows = [_handout_row()]
        db   = _make_db(fetch=AsyncMock(return_value=rows))
        svc  = HandoutService(db=db, gemini_client=_make_gemini())

        result = await svc.get_player_handouts("p1", "c1")

        assert len(result) == 1
        sql = db.fetch.call_args[0][0]
        assert "handout_recipients" in sql

    @pytest.mark.asyncio
    async def test_get_pending_for_player_filters_global(self):
        rows = [_handout_row(is_global=True)]
        db   = _make_db(fetch=AsyncMock(return_value=rows))
        svc  = HandoutService(db=db, gemini_client=_make_gemini())

        result = await svc.get_pending_for_player("p1")

        assert len(result) == 1
        sql = db.fetch.call_args[0][0]
        assert "is_global" in sql.lower()

    @pytest.mark.asyncio
    async def test_get_delivery_status_returns_recipients(self):
        rows = [
            {"player_id": "p1", "delivered_at": None},
            {"player_id": "p2", "delivered_at": None},
        ]
        db  = _make_db(fetch=AsyncMock(return_value=rows))
        svc = HandoutService(db=db, gemini_client=_make_gemini())

        result = await svc.get_delivery_status("h1")

        assert len(result) == 2
        assert result[0]["player_id"] == "p1"

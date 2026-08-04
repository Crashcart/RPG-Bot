"""
Unit tests for orchestrator/services/faction_service.py — FactionService.

All database and Gemini calls are mocked; no live services required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.faction_service import FactionService, _score_label


# ---------------------------------------------------------------------------
# Pure-function tests: _score_label
# ---------------------------------------------------------------------------

class TestScoreLabel:

    @pytest.mark.parametrize("score,expected", [
        (100,  "Allied"),
        (75,   "Allied"),
        (74,   "Friendly"),
        (40,   "Friendly"),
        (39,   "Neutral"),
        (10,   "Neutral"),
        (9,    "Cautious"),
        (-25,  "Cautious"),
        (-26,  "Hostile"),
        (-60,  "Hostile"),
        (-61,  "Enemy"),
        (-100, "Enemy"),
    ])
    def test_all_thresholds(self, score, expected):
        assert _score_label(score) == expected

    def test_zero_is_cautious(self):
        # Neutral threshold is ≥10; 0 falls into the Cautious band (≥-25)
        assert _score_label(0) == "Cautious"

    def test_boundary_allied(self):
        assert _score_label(75) == "Allied"

    def test_boundary_friendly(self):
        assert _score_label(40) == "Friendly"

    def test_below_enemy_floor(self):
        # Anything below -60 is Enemy, including extreme values
        assert _score_label(-99) == "Enemy"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(**overrides):
    db = MagicMock()
    db.fetch    = AsyncMock(return_value=[])
    db.fetchrow = AsyncMock(return_value=None)
    db.fetchval = AsyncMock(return_value="new-faction-uuid")
    db.execute  = AsyncMock()
    for k, v in overrides.items():
        setattr(db, k, v)
    return db


def _make_gemini(response: str = "[]"):
    g = MagicMock()
    g.generate = AsyncMock(return_value=response)
    return g


def _faction_row(name: str, disposition: dict, description: str = ""):
    return {"name": name, "description": description, "disposition": disposition}


def _faction_db_row(faction_id: str, disposition: dict, name: str = "Guards"):
    return {"id": faction_id, "disposition": disposition, "name": name}


# ---------------------------------------------------------------------------
# get_standings
# ---------------------------------------------------------------------------

class TestGetStandings:

    @pytest.mark.asyncio
    async def test_empty_factions_returns_empty_list(self):
        db  = _make_db(fetch=AsyncMock(return_value=[]))
        svc = FactionService(db=db, gemini_client=_make_gemini())
        result = await svc.get_standings("player1", "campaign1")
        assert result == []

    @pytest.mark.asyncio
    async def test_player_with_no_entry_defaults_to_zero(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            _faction_row("Thieves Guild", {}),
        ]))
        svc = FactionService(db=db, gemini_client=_make_gemini())
        standings = await svc.get_standings("player1", "campaign1")
        assert standings[0]["score"] == 0
        assert standings[0]["label"] == "Cautious"  # Neutral requires score ≥ 10

    @pytest.mark.asyncio
    async def test_standings_sorted_descending_by_score(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            _faction_row("Guards",   {"p1": -50}),
            _faction_row("Mages",    {"p1": 80}),
            _faction_row("Merchants", {"p1": 20}),
        ]))
        svc = FactionService(db=db, gemini_client=_make_gemini())
        result = await svc.get_standings("p1", "campaign1")
        scores = [r["score"] for r in result]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_label_applied_correctly_to_each_faction(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            _faction_row("Allies", {"p1": 90}),
            _faction_row("Enemies", {"p1": -80}),
        ]))
        svc = FactionService(db=db, gemini_client=_make_gemini())
        result = await svc.get_standings("p1", "campaign1")
        labels = {r["name"]: r["label"] for r in result}
        assert labels["Allies"]  == "Allied"
        assert labels["Enemies"] == "Enemy"


# ---------------------------------------------------------------------------
# adjust — score clamping
# ---------------------------------------------------------------------------

class TestAdjust:

    @pytest.mark.asyncio
    async def test_adjust_returns_new_score(self):
        row = _faction_db_row("fid-1", {"p1": 50})
        db  = _make_db(fetchrow=AsyncMock(return_value=row))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        score = await svc.adjust("campaign1", "Guards", "p1", delta=20)
        assert score == 70

    @pytest.mark.asyncio
    async def test_adjust_clamps_at_positive_100(self):
        row = _faction_db_row("fid-1", {"p1": 95})
        db  = _make_db(fetchrow=AsyncMock(return_value=row))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        score = await svc.adjust("campaign1", "Guards", "p1", delta=15)
        assert score == 100

    @pytest.mark.asyncio
    async def test_adjust_clamps_at_negative_100(self):
        row = _faction_db_row("fid-1", {"p1": -95})
        db  = _make_db(fetchrow=AsyncMock(return_value=row))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        score = await svc.adjust("campaign1", "Guards", "p1", delta=-15)
        assert score == -100

    @pytest.mark.asyncio
    async def test_adjust_new_player_starts_from_zero(self):
        row = _faction_db_row("fid-1", {})
        db  = _make_db(fetchrow=AsyncMock(return_value=row))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        score = await svc.adjust("campaign1", "Guards", "new-player", delta=10)
        assert score == 10

    @pytest.mark.asyncio
    async def test_adjust_missing_faction_returns_zero(self):
        db  = _make_db(fetchrow=AsyncMock(return_value=None))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        score = await svc.adjust("campaign1", "Ghost Faction", "p1", delta=5)
        assert score == 0
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_adjust_persists_updated_disposition(self):
        row = _faction_db_row("fid-1", {"p1": 20})
        db  = _make_db(fetchrow=AsyncMock(return_value=row))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        await svc.adjust("campaign1", "Guards", "p1", delta=5)

        db.execute.assert_called_once()
        call_args = db.execute.call_args[0]
        # Second positional arg is the serialized disposition JSON
        saved = json.loads(call_args[1])
        assert saved["p1"] == 25


# ---------------------------------------------------------------------------
# upsert_faction
# ---------------------------------------------------------------------------

class TestUpsertFaction:

    @pytest.mark.asyncio
    async def test_creates_new_faction_when_not_found(self):
        db  = _make_db(fetchrow=AsyncMock(return_value=None))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        fid = await svc.upsert_faction("campaign1", "Thieves Guild", "Shady characters")

        assert fid == "new-faction-uuid"
        db.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_existing_faction_description(self):
        existing = {"id": "existing-id"}
        db  = _make_db(fetchrow=AsyncMock(return_value=existing))
        svc = FactionService(db=db, gemini_client=_make_gemini())

        fid = await svc.upsert_faction("campaign1", "Guards", "Updated desc")

        assert fid == "existing-id"
        db.execute.assert_called_once()
        sql = db.execute.call_args[0][0]
        assert "UPDATE" in sql.upper()


# ---------------------------------------------------------------------------
# ai_adjust_from_narrative
# ---------------------------------------------------------------------------

class TestAiAdjustFromNarrative:

    @pytest.mark.asyncio
    async def test_exits_early_when_no_factions(self):
        db = _make_db(fetch=AsyncMock(return_value=[]))
        g  = _make_gemini()
        svc = FactionService(db=db, gemini_client=g)

        await svc.ai_adjust_from_narrative("c1", "p1", "narrative", "combat")
        g.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_applies_adjustments_from_gemini(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            {"id": "f1", "name": "Guards", "description": "City watch"},
        ]))
        gemini_resp = json.dumps([
            {"faction": "Guards", "delta": -10, "reason": "Player fought guards"},
        ])
        g = _make_gemini(response=gemini_resp)

        # Wire fetchrow so adjust() can update the score
        db.fetchrow = AsyncMock(return_value=_faction_db_row("f1", {"p1": 30}, "Guards"))

        svc = FactionService(db=db, gemini_client=g)
        await svc.ai_adjust_from_narrative("c1", "p1", "Player attacked a guard.", "combat")

        db.execute.assert_called()

    @pytest.mark.asyncio
    async def test_strips_json_code_fences(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            {"id": "f1", "name": "Mages", "description": ""},
        ]))
        db.fetchrow = AsyncMock(return_value=_faction_db_row("f1", {}, "Mages"))

        fenced = "```json\n[{\"faction\": \"Mages\", \"delta\": 5, \"reason\": \"helped\"}]\n```"
        g = _make_gemini(response=fenced)

        svc = FactionService(db=db, gemini_client=g)
        # Should not raise even though code fences are present
        await svc.ai_adjust_from_narrative("c1", "p1", "Helped the mages.", "social")
        db.execute.assert_called()

    @pytest.mark.asyncio
    async def test_empty_array_response_applies_no_adjustments(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            {"id": "f1", "name": "Guards", "description": ""},
        ]))
        g = _make_gemini(response="[]")
        svc = FactionService(db=db, gemini_client=g)

        await svc.ai_adjust_from_narrative("c1", "p1", "Player slept.", "rest")
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_gemini_error_does_not_propagate(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            {"id": "f1", "name": "Guards", "description": ""},
        ]))
        g = MagicMock()
        g.generate = AsyncMock(side_effect=Exception("Gemini timeout"))
        svc = FactionService(db=db, gemini_client=g)

        # Should not raise — errors are swallowed in fire-and-forget
        await svc.ai_adjust_from_narrative("c1", "p1", "narrative", "combat")

    @pytest.mark.asyncio
    async def test_invalid_json_response_does_not_propagate(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            {"id": "f1", "name": "Guards", "description": ""},
        ]))
        g = _make_gemini(response="not valid json {{{")
        svc = FactionService(db=db, gemini_client=g)

        await svc.ai_adjust_from_narrative("c1", "p1", "narrative", "combat")
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_narrative_excerpt_truncated_to_800_chars(self):
        db = _make_db(fetch=AsyncMock(return_value=[
            {"id": "f1", "name": "Guards", "description": ""},
        ]))
        g = _make_gemini(response="[]")
        svc = FactionService(db=db, gemini_client=g)

        long_narrative = "x" * 2000
        await svc.ai_adjust_from_narrative("c1", "p1", long_narrative, "combat")

        prompt_arg = g.generate.call_args[1]["user"]
        # The truncated excerpt inside the prompt must be ≤ 800 chars
        assert long_narrative[:800] in prompt_arg
        assert long_narrative[:801] not in prompt_arg

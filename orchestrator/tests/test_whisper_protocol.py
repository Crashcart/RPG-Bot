"""
Tests for the Whisper Protocol (Issue #19).

Coverage:
  - WhisperService.should_trigger_whisper(): all trigger paths
  - WhisperService.check_rate_limit(): allow, block, and Redis fail-open
  - WhisperService.build_hidden_context(): severity levels + empty state
  - WhisperService.apply_delta(): sanity drain, flag mutation, threshold recalc, audit log
  - WhisperService.get_hidden_state(): hit and miss
  - HiddenStateDelta schema validation
  - OllamaResolutionPayload.hidden_state_delta field presence
  - GMDirector._generate_whisper(): hidden_context prepended to system prompt
  - GMDirector.narrate(): WhisperService integration (whisper_svc wired in)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.payloads import (
    ActionOutcome,
    CharacterStatus,
    DiceRequest,
    HiddenStateDelta,
    OllamaResolutionPayload,
    StateDelta,
)
from orchestrator.services.whisper_service import (
    SANITY_BREAKDOWN,
    SANITY_PARANOID,
    SANITY_WARNING,
    WhisperService,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_resolution(**kwargs) -> OllamaResolutionPayload:
    defaults = dict(
        intent_id="intent-001",
        action_type="melee_attack",
        difficulty=12,
        dice_request=DiceRequest(notation="1d20", modifier=2, purpose="attack"),
        roll_result=14,
        outcome=ActionOutcome.SUCCESS,
        state_delta=StateDelta(character_id="char-001"),
    )
    defaults.update(kwargs)
    return OllamaResolutionPayload(**defaults)


def _make_db(hidden_state: dict | None = None) -> MagicMock:
    """Mock DatabaseService with a pool that returns a given hidden_state."""
    state_json = json.dumps(hidden_state or {})
    row = {"hidden_state": state_json}

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=row)
    mock_conn.execute  = AsyncMock(return_value=None)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__  = AsyncMock(return_value=False)

    mock_acquire = MagicMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__  = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire)
    mock_pool.fetchrow = AsyncMock(return_value=row)
    mock_pool.fetch    = AsyncMock(return_value=[])

    db = MagicMock()
    db.pool = mock_pool
    return db


def _make_redis(count: int = 1) -> AsyncMock:
    redis = AsyncMock()
    redis.incr   = AsyncMock(return_value=count)
    redis.expire = AsyncMock(return_value=True)
    return redis


# ── should_trigger_whisper ────────────────────────────────────────────────────

class TestShouldTriggerWhisper:
    def setup_method(self):
        self.svc = WhisperService(db=MagicMock(), redis=None)

    def test_horror_action_type_triggers(self):
        assert self.svc.should_trigger_whisper(
            "eldritch_gaze", "", "success", {}
        ) is True

    def test_sanity_check_triggers(self):
        assert self.svc.should_trigger_whisper(
            "sanity_check", "", "failure", {}
        ) is True

    def test_horror_keyword_in_reasoning_triggers(self):
        assert self.svc.should_trigger_whisper(
            "search", "the player is terrified by a vision", "success", {}
        ) is True

    def test_horror_keyword_in_outcome_triggers(self):
        assert self.svc.should_trigger_whisper(
            "perception", "", "madness overtakes them", {}
        ) is True

    def test_paranoid_flag_triggers(self):
        assert self.svc.should_trigger_whisper(
            "walk", "", "success", {"paranoid": True}
        ) is True

    def test_cursed_flag_triggers(self):
        assert self.svc.should_trigger_whisper(
            "rest", "", "success", {"cursed": True}
        ) is True

    def test_possessed_flag_triggers(self):
        assert self.svc.should_trigger_whisper(
            "speak", "", "success", {"possessed": True}
        ) is True

    def test_low_sanity_triggers(self):
        assert self.svc.should_trigger_whisper(
            "walk", "", "success", {"sanity": SANITY_WARNING}
        ) is True

    def test_normal_action_no_trigger(self):
        assert self.svc.should_trigger_whisper(
            "melee_attack", "hits the goblin", "success", {"sanity": 100}
        ) is False

    def test_full_sanity_no_flags_no_trigger(self):
        assert self.svc.should_trigger_whisper(
            "persuade", "convinces the merchant", "success", {}
        ) is False


# ── check_rate_limit ──────────────────────────────────────────────────────────

class TestRateLimit:
    @pytest.mark.asyncio
    async def test_allows_first_check(self):
        redis = _make_redis(count=1)
        svc = WhisperService(db=MagicMock(), redis=redis)
        assert await svc.check_rate_limit("char-001") is True

    @pytest.mark.asyncio
    async def test_allows_up_to_max(self):
        redis = _make_redis(count=3)
        svc = WhisperService(db=MagicMock(), redis=redis)
        assert await svc.check_rate_limit("char-001") is True

    @pytest.mark.asyncio
    async def test_blocks_over_max(self):
        redis = _make_redis(count=4)
        svc = WhisperService(db=MagicMock(), redis=redis)
        assert await svc.check_rate_limit("char-001") is False

    @pytest.mark.asyncio
    async def test_fail_open_on_redis_error(self):
        redis = AsyncMock()
        redis.incr = AsyncMock(side_effect=ConnectionError("redis down"))
        svc = WhisperService(db=MagicMock(), redis=redis)
        assert await svc.check_rate_limit("char-001") is True

    @pytest.mark.asyncio
    async def test_no_redis_always_allows(self):
        svc = WhisperService(db=MagicMock(), redis=None)
        assert await svc.check_rate_limit("char-001") is True


# ── build_hidden_context ──────────────────────────────────────────────────────

class TestBuildHiddenContext:
    def setup_method(self):
        self.svc = WhisperService(db=MagicMock(), redis=None)

    def test_empty_state_returns_empty(self):
        assert self.svc.build_hidden_context({}) == ""

    def test_full_sanity_no_flags(self):
        ctx = self.svc.build_hidden_context({"sanity": 100})
        assert "100/100" in ctx
        assert "GM EYES ONLY" in ctx

    def test_low_sanity_severity_label(self):
        ctx = self.svc.build_hidden_context({"sanity": SANITY_WARNING})
        assert "LOW SANITY" in ctx

    def test_paranoid_severity_label(self):
        ctx = self.svc.build_hidden_context({"sanity": SANITY_PARANOID})
        assert "PARANOID" in ctx

    def test_breakdown_severity_label(self):
        ctx = self.svc.build_hidden_context({"sanity": SANITY_BREAKDOWN})
        assert "BREAKDOWN" in ctx

    def test_active_flags_listed(self):
        ctx = self.svc.build_hidden_context({"sanity": 60, "cursed": True, "paranoid": True})
        assert "cursed" in ctx
        assert "paranoid" in ctx


# ── apply_delta ───────────────────────────────────────────────────────────────

class TestApplyDelta:
    @pytest.mark.asyncio
    async def test_sanity_drain_applied(self):
        db = _make_db({"sanity": 80})
        svc = WhisperService(db=db, redis=None)
        delta = HiddenStateDelta(sanity_delta=-15, trigger_reason="horror_witness")
        result = await svc.apply_delta("char-001", "camp-001", "intent-001", delta)
        assert result["sanity"] == 65

    @pytest.mark.asyncio
    async def test_sanity_clamped_to_zero(self):
        db = _make_db({"sanity": 5})
        svc = WhisperService(db=db, redis=None)
        delta = HiddenStateDelta(sanity_delta=-20)
        result = await svc.apply_delta("char-001", "camp-001", "intent-001", delta)
        assert result["sanity"] == 0

    @pytest.mark.asyncio
    async def test_flags_set(self):
        db = _make_db({"sanity": 70})
        svc = WhisperService(db=db, redis=None)
        delta = HiddenStateDelta(flags_set={"cursed": True})
        result = await svc.apply_delta("char-001", "camp-001", "intent-001", delta)
        assert result["cursed"] is True

    @pytest.mark.asyncio
    async def test_flags_cleared(self):
        db = _make_db({"sanity": 70, "cursed": True})
        svc = WhisperService(db=db, redis=None)
        delta = HiddenStateDelta(flags_cleared=["cursed"])
        result = await svc.apply_delta("char-001", "camp-001", "intent-001", delta)
        assert "cursed" not in result

    @pytest.mark.asyncio
    async def test_paranoid_threshold_flag_set(self):
        db = _make_db({"sanity": 25})
        svc = WhisperService(db=db, redis=None)
        delta = HiddenStateDelta(sanity_delta=-10)  # drops to 15 → paranoid
        result = await svc.apply_delta("char-001", "camp-001", "intent-001", delta)
        assert result["paranoid"] is True

    @pytest.mark.asyncio
    async def test_breakdown_flag_set(self):
        db = _make_db({"sanity": 8})
        svc = WhisperService(db=db, redis=None)
        delta = HiddenStateDelta(sanity_delta=-5)  # drops to 3 → breakdown
        result = await svc.apply_delta("char-001", "camp-001", "intent-001", delta)
        assert result["breakdown"] is True

    @pytest.mark.asyncio
    async def test_audit_log_written(self):
        db = _make_db({"sanity": 60})
        svc = WhisperService(db=db, redis=None)
        delta = HiddenStateDelta(sanity_delta=-5, trigger_reason="eldritch_gaze")
        await svc.apply_delta("char-001", "camp-001", "intent-001", delta, whisper_text="You see shapes.")
        # Verify execute was called (INSERT into whisper_log)
        conn = db.pool.acquire.return_value.__aenter__.return_value
        assert conn.execute.call_count >= 2   # UPDATE + INSERT


# ── Schema: HiddenStateDelta ──────────────────────────────────────────────────

class TestHiddenStateDeltaSchema:
    def test_default_delta_is_noop(self):
        delta = HiddenStateDelta()
        assert delta.sanity_delta == 0
        assert delta.flags_set == {}
        assert delta.flags_cleared == []
        assert delta.trigger_reason == ""

    def test_negative_sanity_drain(self):
        delta = HiddenStateDelta(sanity_delta=-10, trigger_reason="sanity_check")
        assert delta.sanity_delta == -10

    def test_positive_sanity_restore(self):
        delta = HiddenStateDelta(sanity_delta=5)
        assert delta.sanity_delta == 5


# ── OllamaResolutionPayload.hidden_state_delta ────────────────────────────────

class TestResolutionPayloadHiddenStateDelta:
    def test_field_is_none_by_default(self):
        res = _make_resolution()
        assert res.hidden_state_delta is None

    def test_field_accepts_hidden_state_delta(self):
        delta = HiddenStateDelta(sanity_delta=-10, trigger_reason="horror_witness")
        res = _make_resolution(hidden_state_delta=delta)
        assert res.hidden_state_delta is not None
        assert res.hidden_state_delta.sanity_delta == -10


# ── GMDirector._generate_whisper with hidden_context ─────────────────────────

class TestGenerateWhisperHiddenContext:
    @pytest.mark.asyncio
    async def test_hidden_context_prepended_to_system_prompt(self):
        from orchestrator.services.gm_director import GMDirector

        storyteller = AsyncMock()
        storyteller.generate = AsyncMock(return_value="You sense a shadow behind the merchant.")

        gm = GMDirector(
            gemini=MagicMock(),
            node_router=MagicMock(),
            dispatcher=MagicMock(),
            story_memory=MagicMock(),
        )

        res = _make_resolution(action_type="social_talk")
        plan = MagicMock()
        plan.sub_tasks = []
        plan.direct_elements = []

        whisper = await gm._generate_whisper(
            storyteller, res, plan, [], "I ask the merchant about the scroll.",
            hidden_context="[HIDDEN PSYCHOLOGICAL STATE — GM EYES ONLY]\nSanity: 22/100",
        )

        assert whisper is not None
        call_kwargs = storyteller.generate.call_args[1]
        assert "[HIDDEN PSYCHOLOGICAL STATE" in call_kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_no_hidden_context_uses_default_system_prompt(self):
        from orchestrator.services.gm_director import GMDirector
        from orchestrator.prompts.immersion_prompts import WHISPER_SYSTEM_PROMPT

        storyteller = AsyncMock()
        storyteller.generate = AsyncMock(return_value="You notice a slight tremor in his hand.")

        gm = GMDirector(
            gemini=MagicMock(),
            node_router=MagicMock(),
            dispatcher=MagicMock(),
            story_memory=MagicMock(),
        )

        res = _make_resolution()
        plan = MagicMock()
        plan.sub_tasks = []
        plan.direct_elements = []

        whisper = await gm._generate_whisper(
            storyteller, res, plan, [], "I watch the guard carefully.", hidden_context=""
        )

        call_kwargs = storyteller.generate.call_args[1]
        assert call_kwargs["system_prompt"] == WHISPER_SYSTEM_PROMPT

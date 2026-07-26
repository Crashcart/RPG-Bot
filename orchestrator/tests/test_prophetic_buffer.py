"""
Unit tests for orchestrator/services/prophetic_buffer.py

Coverage targets:
  - _FOLLOW_UP_MAP completeness (all ActionOutcome values mapped)
  - _AMBIENT_PREDICTION mapping
  - enqueue() — happy path and backpressure drop
  - get_prefetched_text / get_prefetched_audio — cache hit, miss, and error
  - _prefetch() — audio pre-selection, text generation, timeout handling
  - _cache_set() — success and failure (fail-open)
  - Lifecycle: start() and stop()
  - is_busy property
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from orchestrator.schemas.payloads import ActionOutcome
from orchestrator.services.prophetic_buffer import (
    PropheticBuffer,
    _AMBIENT_PREDICTION,
    _FOLLOW_UP_MAP,
    _PREFETCH_TTL,
    _MAX_QUEUE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_pipeline_result(outcome: str = "success", intent_id: str = "test-intent-001"):
    """Return a minimal PipelineResult-like object suitable for enqueue()."""
    result = MagicMock()
    result.resolution.outcome.value = outcome
    result.resolution.action_type = "melee_attack"
    result.intent.intent_id = intent_id
    result.narrative.narrative = "The blade connects with a resonant clang." * 3
    return result


def _make_buffer(cache=None, storyteller=None):
    cache = cache or AsyncMock()
    storyteller = storyteller or AsyncMock()
    return PropheticBuffer(cache=cache, storyteller=storyteller)


# ─────────────────────────────────────────────────────────────────────────────
# Static mapping tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFollowUpMap:
    def test_all_action_outcomes_covered(self):
        """Every ActionOutcome value must have an entry in _FOLLOW_UP_MAP."""
        for member in ActionOutcome:
            assert member.value in _FOLLOW_UP_MAP, (
                f"ActionOutcome.{member.name} ({member.value!r}) missing from _FOLLOW_UP_MAP"
            )

    def test_each_outcome_has_at_least_one_follow_up(self):
        for outcome, follow_ups in _FOLLOW_UP_MAP.items():
            assert len(follow_ups) >= 1, f"Outcome {outcome!r} has empty follow-up list"

    def test_follow_ups_are_strings(self):
        for outcome, follow_ups in _FOLLOW_UP_MAP.items():
            for fu in follow_ups:
                assert isinstance(fu, str), f"Non-string follow-up in {outcome!r}: {fu!r}"

    def test_critical_success_includes_press_advantage(self):
        assert "press_advantage" in _FOLLOW_UP_MAP["critical_success"]

    def test_critical_failure_includes_flee_or_emergency(self):
        assert any(fu in _FOLLOW_UP_MAP["critical_failure"] for fu in ("flee", "emergency_response"))


class TestAmbientPrediction:
    def test_combat_outcomes_map_to_combat_tension(self):
        combat_keys = {"press_advantage", "emergency_response", "flee", "defensive_action"}
        for key in combat_keys:
            if key in _AMBIENT_PREDICTION:
                assert _AMBIENT_PREDICTION[key] == "combat_tension", (
                    f"{key!r} should map to 'combat_tension'"
                )

    def test_social_interaction_maps_to_tavern_chatter(self):
        assert _AMBIENT_PREDICTION.get("social_interaction") == "tavern_chatter"

    def test_move_to_next_area_maps_to_dungeon_ambience(self):
        assert _AMBIENT_PREDICTION.get("move_to_next_area") == "dungeon_ambience"

    def test_recover_and_regroup_map_to_campfire(self):
        assert _AMBIENT_PREDICTION.get("recover") == "campfire_quiet"
        assert _AMBIENT_PREDICTION.get("regroup") == "campfire_quiet"


# ─────────────────────────────────────────────────────────────────────────────
# enqueue() tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_adds_to_queue(self):
        buf = _make_buffer()
        result = _make_pipeline_result()
        await buf.enqueue(result)
        assert buf._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_drops_silently_when_queue_full(self):
        buf = _make_buffer()
        # Fill the queue to capacity
        for i in range(_MAX_QUEUE):
            await buf.enqueue(_make_pipeline_result(intent_id=f"intent-{i}"))
        assert buf._queue.qsize() == _MAX_QUEUE
        # One more should be dropped, not raise
        await buf.enqueue(_make_pipeline_result(intent_id="overflow"))
        assert buf._queue.qsize() == _MAX_QUEUE  # unchanged

    @pytest.mark.asyncio
    async def test_enqueue_non_blocking(self):
        """enqueue() must return without waiting for prefetch to complete."""
        buf = _make_buffer()
        result = _make_pipeline_result()
        # Should complete almost instantly
        await asyncio.wait_for(buf.enqueue(result), timeout=0.1)


# ─────────────────────────────────────────────────────────────────────────────
# Cache read helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestCacheReads:
    @pytest.mark.asyncio
    async def test_get_prefetched_text_returns_cached_value(self):
        cache = AsyncMock()
        cache.get.return_value = "The shadows deepen around you."
        buf = _make_buffer(cache=cache)
        result = await buf.get_prefetched_text("intent-abc")
        cache.get.assert_called_once_with("ironclad:prophet:intent-abc:text")
        assert result == "The shadows deepen around you."

    @pytest.mark.asyncio
    async def test_get_prefetched_text_returns_none_on_cache_miss(self):
        cache = AsyncMock()
        cache.get.return_value = None
        buf = _make_buffer(cache=cache)
        result = await buf.get_prefetched_text("intent-xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_prefetched_text_returns_none_on_exception(self):
        cache = AsyncMock()
        cache.get.side_effect = RuntimeError("Redis unavailable")
        buf = _make_buffer(cache=cache)
        result = await buf.get_prefetched_text("intent-err")
        assert result is None  # fail-open

    @pytest.mark.asyncio
    async def test_get_prefetched_audio_returns_cached_key(self):
        cache = AsyncMock()
        cache.get.return_value = "combat_tension"
        buf = _make_buffer(cache=cache)
        result = await buf.get_prefetched_audio("intent-audio")
        cache.get.assert_called_once_with("ironclad:prophet:intent-audio:audio")
        assert result == "combat_tension"

    @pytest.mark.asyncio
    async def test_get_prefetched_audio_returns_none_on_miss(self):
        cache = AsyncMock()
        cache.get.return_value = None
        buf = _make_buffer(cache=cache)
        assert await buf.get_prefetched_audio("miss") is None

    @pytest.mark.asyncio
    async def test_get_prefetched_audio_returns_none_on_exception(self):
        cache = AsyncMock()
        cache.get.side_effect = ConnectionError("Redis down")
        buf = _make_buffer(cache=cache)
        assert await buf.get_prefetched_audio("err") is None


# ─────────────────────────────────────────────────────────────────────────────
# _cache_set tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCacheSet:
    @pytest.mark.asyncio
    async def test_cache_set_calls_set_with_correct_ttl(self):
        cache = AsyncMock()
        buf = _make_buffer(cache=cache)
        await buf._cache_set("ironclad:prophet:x:text", "hello")
        cache.set.assert_called_once_with("ironclad:prophet:x:text", "hello", ttl=_PREFETCH_TTL)

    @pytest.mark.asyncio
    async def test_cache_set_swallows_exceptions(self):
        cache = AsyncMock()
        cache.set.side_effect = OSError("disk full")
        buf = _make_buffer(cache=cache)
        # Must not raise
        await buf._cache_set("key", "value")


# ─────────────────────────────────────────────────────────────────────────────
# _prefetch() tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPrefetch:
    @pytest.mark.asyncio
    async def test_prefetch_writes_audio_key_for_known_outcome(self):
        cache = AsyncMock()
        storyteller = AsyncMock()
        storyteller.generate.return_value = "Dust settles over the battlefield."
        buf = _make_buffer(cache=cache, storyteller=storyteller)

        result = _make_pipeline_result(outcome="critical_success", intent_id="intent-cs")
        await buf._prefetch(result)

        # Audio key for critical_success → press_advantage → combat_tension
        audio_call = next(
            call for call in cache.set.call_args_list
            if "audio" in call.args[0]
        )
        assert audio_call.args[0] == "ironclad:prophet:intent-cs:audio"
        assert audio_call.args[1] == "combat_tension"

    @pytest.mark.asyncio
    async def test_prefetch_writes_text_snippet_on_storyteller_success(self):
        cache = AsyncMock()
        storyteller = AsyncMock()
        storyteller.generate.return_value = "The echo of steel fades into silence."
        buf = _make_buffer(cache=cache, storyteller=storyteller)

        result = _make_pipeline_result(outcome="success", intent_id="intent-s")
        await buf._prefetch(result)

        text_call = next(
            call for call in cache.set.call_args_list
            if "text" in call.args[0]
        )
        assert text_call.args[0] == "ironclad:prophet:intent-s:text"
        assert text_call.args[1] == "The echo of steel fades into silence."

    @pytest.mark.asyncio
    async def test_prefetch_skips_text_when_storyteller_returns_empty(self):
        cache = AsyncMock()
        storyteller = AsyncMock()
        storyteller.generate.return_value = ""
        buf = _make_buffer(cache=cache, storyteller=storyteller)

        result = _make_pipeline_result(outcome="failure", intent_id="intent-f")
        await buf._prefetch(result)

        text_calls = [c for c in cache.set.call_args_list if "text" in c.args[0]]
        assert len(text_calls) == 0  # no snippet cached for empty output

    @pytest.mark.asyncio
    async def test_prefetch_handles_timeout_gracefully(self):
        cache = AsyncMock()
        storyteller = AsyncMock()

        async def slow_generate(**kwargs):
            await asyncio.sleep(999)

        storyteller.generate.side_effect = slow_generate
        buf = _make_buffer(cache=cache, storyteller=storyteller)

        result = _make_pipeline_result(outcome="success", intent_id="intent-timeout")
        # Patch the timeout to fire immediately
        with patch("orchestrator.services.prophetic_buffer._PREFETCH_TIMEOUT", 0.01):
            await buf._prefetch(result)  # must not raise

    @pytest.mark.asyncio
    async def test_prefetch_no_audio_for_unknown_follow_up(self):
        """If a follow-up key is not in _AMBIENT_PREDICTION, no audio is cached."""
        cache = AsyncMock()
        storyteller = AsyncMock()
        storyteller.generate.return_value = "Silence."
        buf = _make_buffer(cache=cache, storyteller=storyteller)

        # partial_success → recover → campfire_quiet (IS in map)
        # critical_failure → emergency_response → combat_tension (IS in map)
        # Let's test with a custom outcome that maps to something not in _AMBIENT_PREDICTION
        with patch("orchestrator.services.prophetic_buffer._FOLLOW_UP_MAP",
                   {"custom_outcome": ["assess_situation"]}):
            result = _make_pipeline_result(outcome="custom_outcome", intent_id="intent-unk")
            # assess_situation is NOT in _AMBIENT_PREDICTION → no audio write
            await buf._prefetch(result)

        audio_calls = [c for c in cache.set.call_args_list if "audio" in c.args[0]]
        assert len(audio_calls) == 0

    @pytest.mark.asyncio
    async def test_prefetch_uses_first_follow_up_as_primary(self):
        """Primary follow-up is always follow_ups[0]."""
        cache = AsyncMock()
        storyteller = AsyncMock()
        storyteller.generate.return_value = "snippet"
        buf = _make_buffer(cache=cache, storyteller=storyteller)

        with patch("orchestrator.services.prophetic_buffer._FOLLOW_UP_MAP",
                   {"success": ["social_interaction", "move_to_next_area"]}):
            result = _make_pipeline_result(outcome="success", intent_id="intent-primary")
            await buf._prefetch(result)

        # social_interaction → tavern_chatter (not dungeon_ambience from second)
        audio_calls = [c for c in cache.set.call_args_list if "audio" in c.args[0]]
        assert audio_calls[0].args[1] == "tavern_chatter"

    @pytest.mark.asyncio
    async def test_prefetch_uses_default_when_outcome_missing(self):
        """Unknown outcome falls back to ['assess_situation']."""
        cache = AsyncMock()
        storyteller = AsyncMock()
        storyteller.generate.return_value = "snippet"
        buf = _make_buffer(cache=cache, storyteller=storyteller)

        result = _make_pipeline_result(outcome="unknown_outcome", intent_id="intent-default")
        await buf._prefetch(result)

        # assess_situation not in _AMBIENT_PREDICTION → no audio write
        audio_calls = [c for c in cache.set.call_args_list if "audio" in c.args[0]]
        assert len(audio_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle + is_busy
# ─────────────────────────────────────────────────────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        buf = _make_buffer()
        await buf.start()
        assert buf._task is not None
        assert not buf._task.done()
        await buf.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        buf = _make_buffer()
        await buf.start()
        task = buf._task
        await buf.stop()
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_is_busy_false_when_idle(self):
        buf = _make_buffer()
        assert not buf.is_busy

    @pytest.mark.asyncio
    async def test_is_busy_true_during_prefetch(self):
        prefetch_started = asyncio.Event()
        prefetch_gate    = asyncio.Event()

        async def slow_prefetch(result):
            buf._busy = True
            prefetch_started.set()
            await prefetch_gate.wait()
            buf._busy = False

        buf = _make_buffer()
        with patch.object(buf, "_prefetch", side_effect=slow_prefetch):
            await buf.start()
            await buf.enqueue(_make_pipeline_result())
            await asyncio.wait_for(prefetch_started.wait(), timeout=1.0)
            assert buf.is_busy
            prefetch_gate.set()
            await asyncio.sleep(0.05)
        await buf.stop()

    @pytest.mark.asyncio
    async def test_worker_continues_after_prefetch_exception(self):
        """The worker loop must not die on a prefetch error."""
        call_count = 0

        async def failing_then_ok(result):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated crash")

        buf = _make_buffer()
        with patch.object(buf, "_prefetch", side_effect=failing_then_ok):
            await buf.start()
            await buf.enqueue(_make_pipeline_result(intent_id="intent-1"))
            await buf.enqueue(_make_pipeline_result(intent_id="intent-2"))
            await asyncio.sleep(0.1)  # allow both to process
        await buf.stop()

        assert call_count == 2  # worker survived the first crash

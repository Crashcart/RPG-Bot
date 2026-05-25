"""
Tests for PropheticBuffer — Speculative Narrative Pre-Computation (issue #14).

Covers:
  - Keyword tokenisation and stopword removal
  - Branch keyword scoring (precision-based overlap)
  - Best-match selection across multiple branches
  - Branch prediction map correctness
  - Load-aware branch count (psutil mock)
  - get_speculative_response: cache hit, cache miss, disabled engine,
    corrupt cache, Redis errors, always-delete side-effect
  - Legacy prefetch API backward compatibility
  - Enqueue non-blocking backpressure
  - Ambient audio prediction labels
  - Full _prefetch integration with mocked storyteller
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.prophetic_buffer import (
    BranchEntry,
    PropheticBuffer,
    _AMBIENT_PREDICTION,
    _BRANCH_KEYWORDS,
    _FOLLOW_UP_MAP,
)
from orchestrator.schemas.payloads import (
    ActionOutcome,
    DiceRequest,
    IntentPayload,
    NarrativeResponsePayload,
    OllamaResolutionPayload,
    PipelineResult,
    StateCommitPayload,
    StateDelta,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_pipeline_result(
    outcome: ActionOutcome = ActionOutcome.SUCCESS,
    action_type: str = "melee_attack",
    guild_id: str = "guild-test-001",
    intent_id: str = "intent-test-001",
) -> PipelineResult:
    intent = IntentPayload(
        intent_id=intent_id,
        player_id="player-001",
        guild_id=guild_id,
        channel_id="channel-001",
        session_token="session-001",
        raw_input="I attack the goblin",
    )
    resolution = OllamaResolutionPayload(
        intent_id=intent_id,
        action_type=action_type,
        difficulty=14,
        dice_request=DiceRequest(notation="1d20", modifier=3, purpose="attack roll"),
        roll_result=17,
        outcome=outcome,
        state_delta=StateDelta(character_id="char-001"),
    )
    commit = StateCommitPayload(
        intent_id=intent_id,
        character_id="char-001",
        pre_state={},
        post_state={},
    )
    narrative = NarrativeResponsePayload(
        prompt_id="prompt-001",
        intent_id=intent_id,
        narrative="The goblin staggers back.",
    )
    return PipelineResult(
        intent=intent,
        resolution=resolution,
        commit=commit,
        narrative=narrative,
    )


def _make_buffer(
    storyteller=None,
    settings=None,
):
    cache = MagicMock()
    cache.get    = AsyncMock(return_value=None)
    cache.set    = AsyncMock()
    cache.delete = AsyncMock()
    if storyteller is None:
        storyteller = MagicMock()
        storyteller.generate = AsyncMock(return_value="Some atmospheric prose.")
    return PropheticBuffer(cache=cache, storyteller=storyteller, settings=settings), cache


def _make_settings(**kwargs):
    defaults = {
        "speculative_engine_enabled":        True,
        "speculative_branches":              3,
        "speculative_ttl_seconds":           300,
        "speculative_similarity_threshold":  0.30,
        "speculative_cpu_disable":           85,
        "speculative_cpu_scale_down":        70,
        "speculative_ram_disable":           90,
        "speculative_ram_scale_down":        80,
    }
    defaults.update(kwargs)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


def _sample_branches() -> list[BranchEntry]:
    return [
        BranchEntry(label="press_advantage", narrative_text="You press forward.",   ambient_audio_key="combat_tension"),
        BranchEntry(label="flee",            narrative_text="You turn and flee.",    ambient_audio_key="combat_tension"),
        BranchEntry(label="loot_search",     narrative_text="You search the body.",  ambient_audio_key="dungeon_ambience"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Keyword Tokenisation
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenise:
    def test_extracts_meaningful_words(self):
        tokens = PropheticBuffer._tokenise("I attack the goblin")
        assert "attack" in tokens
        assert "goblin" in tokens

    def test_removes_stopwords(self):
        tokens = PropheticBuffer._tokenise("I attack the goblin")
        assert "i" not in tokens
        assert "the" not in tokens

    def test_lowercases_input(self):
        tokens = PropheticBuffer._tokenise("ATTACK Goblin")
        assert "attack" in tokens
        assert "goblin" in tokens

    def test_empty_string_returns_empty_frozenset(self):
        assert PropheticBuffer._tokenise("") == frozenset()

    def test_only_stopwords_returns_empty(self):
        assert PropheticBuffer._tokenise("I am in the") == frozenset()

    def test_punctuation_stripped(self):
        tokens = PropheticBuffer._tokenise("Run! Flee, now!")
        assert "run" in tokens
        assert "flee" in tokens


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Branch Keyword Scoring
# ─────────────────────────────────────────────────────────────────────────────

class TestScoreBranch:
    def test_matching_token_gives_positive_score(self):
        score = PropheticBuffer._score_branch(frozenset({"attack", "swing"}), "press_advantage")
        assert score > 0.0

    def test_no_match_returns_zero(self):
        score = PropheticBuffer._score_branch(frozenset({"rest", "sleep"}), "press_advantage")
        assert score == 0.0

    def test_empty_player_tokens_returns_zero(self):
        score = PropheticBuffer._score_branch(frozenset(), "press_advantage")
        assert score == 0.0

    def test_unknown_branch_returns_zero(self):
        score = PropheticBuffer._score_branch(frozenset({"attack"}), "nonexistent_label")
        assert score == 0.0

    def test_flee_keywords_score_flee_branch(self):
        score = PropheticBuffer._score_branch(frozenset({"run", "flee"}), "flee")
        assert score > 0.0

    def test_score_bounded_between_zero_and_one(self):
        tokens = frozenset({"attack", "swing", "hit", "kill"})
        for label in _BRANCH_KEYWORDS:
            score = PropheticBuffer._score_branch(tokens, label)
            assert 0.0 <= score <= 1.0, f"Out-of-range score {score} for label {label}"

    def test_perfect_overlap_returns_one(self):
        # Single token that exactly matches the keyword set
        score = PropheticBuffer._score_branch(frozenset({"flee"}), "flee")
        assert score == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Best-Match Selection
# ─────────────────────────────────────────────────────────────────────────────

class TestBestMatch:
    def test_combat_input_selects_press_advantage(self):
        entry, score = PropheticBuffer._best_match("I swing my sword and attack", _sample_branches())
        assert entry is not None
        assert entry["label"] == "press_advantage"
        assert score > 0.0

    def test_flee_input_selects_flee(self):
        entry, score = PropheticBuffer._best_match("I run and flee from danger", _sample_branches())
        assert entry is not None
        assert entry["label"] == "flee"

    def test_loot_input_selects_loot_search(self):
        entry, score = PropheticBuffer._best_match("I search and loot the body", _sample_branches())
        assert entry is not None
        assert entry["label"] == "loot_search"

    def test_empty_branches_returns_none_zero(self):
        entry, score = PropheticBuffer._best_match("attack", [])
        assert entry is None
        assert score == 0.0

    def test_empty_input_returns_zero_score(self):
        entry, score = PropheticBuffer._best_match("", _sample_branches())
        assert score == 0.0

    def test_returns_highest_scoring_branch(self):
        entry, score = PropheticBuffer._best_match(
            "I attack and swing fiercely striking hard", _sample_branches()
        )
        assert entry is not None
        assert entry["label"] == "press_advantage"


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Branch Prediction Map
# ─────────────────────────────────────────────────────────────────────────────

class TestBranchPrediction:
    def test_all_action_outcomes_covered(self):
        for outcome in ActionOutcome:
            assert outcome.value in _FOLLOW_UP_MAP, f"Missing _FOLLOW_UP_MAP entry for {outcome.value}"

    def test_each_outcome_predicts_three_branches(self):
        for outcome, branches in _FOLLOW_UP_MAP.items():
            assert len(branches) == 3, f"Expected 3 branches for '{outcome}', got {len(branches)}"

    def test_critical_success_includes_press_advantage(self):
        assert "press_advantage" in _FOLLOW_UP_MAP["critical_success"]

    def test_critical_failure_includes_emergency_response(self):
        assert "emergency_response" in _FOLLOW_UP_MAP["critical_failure"]

    def test_all_predicted_labels_have_keyword_sets(self):
        all_labels = {lbl for labels in _FOLLOW_UP_MAP.values() for lbl in labels}
        for label in all_labels:
            assert label in _BRANCH_KEYWORDS, f"No keyword set for predicted branch label '{label}'"


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Load-Aware Branch Count
# ─────────────────────────────────────────────────────────────────────────────

class TestEffectiveBranchCount:
    def test_psutil_unavailable_returns_max(self):
        buf, _ = _make_buffer(settings=_make_settings(speculative_branches=3))
        with patch.dict("sys.modules", {"psutil": None}):
            assert buf._effective_branch_count() == 3

    def test_low_load_returns_max(self):
        buf, _ = _make_buffer(settings=_make_settings(speculative_branches=3))
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 30.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=40.0)
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert buf._effective_branch_count() == 3

    def test_moderate_cpu_returns_two(self):
        settings = _make_settings(
            speculative_branches=3,
            speculative_cpu_scale_down=70,
            speculative_cpu_disable=85,
            speculative_ram_scale_down=80,
            speculative_ram_disable=90,
        )
        buf, _ = _make_buffer(settings=settings)
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 75.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=50.0)
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert buf._effective_branch_count() == 2

    def test_heavy_cpu_returns_one(self):
        settings = _make_settings(
            speculative_branches=3,
            speculative_cpu_scale_down=70,
            speculative_cpu_disable=85,
            speculative_ram_scale_down=80,
            speculative_ram_disable=90,
        )
        buf, _ = _make_buffer(settings=settings)
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 90.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=50.0)
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert buf._effective_branch_count() == 1

    def test_heavy_ram_returns_one(self):
        settings = _make_settings(
            speculative_branches=3,
            speculative_cpu_disable=85,
            speculative_ram_disable=90,
        )
        buf, _ = _make_buffer(settings=settings)
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 30.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=92.0)
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert buf._effective_branch_count() == 1

    def test_moderate_ram_returns_two(self):
        settings = _make_settings(
            speculative_branches=3,
            speculative_cpu_scale_down=70,
            speculative_cpu_disable=85,
            speculative_ram_scale_down=80,
            speculative_ram_disable=90,
        )
        buf, _ = _make_buffer(settings=settings)
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 30.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=82.0)
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert buf._effective_branch_count() == 2

    def test_psutil_runtime_error_returns_max(self):
        buf, _ = _make_buffer(settings=_make_settings(speculative_branches=3))
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.side_effect = RuntimeError("psutil failed")
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert buf._effective_branch_count() == 3

    def test_no_settings_defaults_to_three(self):
        buf, _ = _make_buffer(settings=None)
        with patch.dict("sys.modules", {"psutil": None}):
            assert buf._effective_branch_count() == 3


# ─────────────────────────────────────────────────────────────────────────────
# Tests — get_speculative_response
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSpeculativeResponse:
    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(return_value=None)
        result = await buf.get_speculative_response("guild-001", "I attack")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit_good_match_returns_branch(self):
        branches = [
            {"label": "press_advantage", "narrative_text": "You press forward.", "ambient_audio_key": "combat_tension"},
            {"label": "flee",            "narrative_text": "You flee.",          "ambient_audio_key": "combat_tension"},
        ]
        buf, cache = _make_buffer(settings=_make_settings(speculative_similarity_threshold=0.20))
        cache.get = AsyncMock(return_value=json.dumps(branches))
        result = await buf.get_speculative_response("guild-001", "I swing and attack the enemy")
        assert result is not None
        assert result["label"] == "press_advantage"

    @pytest.mark.asyncio
    async def test_cache_hit_poor_match_returns_none(self):
        branches = [
            {"label": "press_advantage", "narrative_text": "You press forward.", "ambient_audio_key": "combat_tension"},
        ]
        buf, cache = _make_buffer(settings=_make_settings(speculative_similarity_threshold=0.90))
        cache.get = AsyncMock(return_value=json.dumps(branches))
        result = await buf.get_speculative_response("guild-001", "completely unrelated words")
        assert result is None

    @pytest.mark.asyncio
    async def test_engine_disabled_returns_none(self):
        branches = [
            {"label": "press_advantage", "narrative_text": "You press forward.", "ambient_audio_key": "combat_tension"},
        ]
        buf, cache = _make_buffer(settings=_make_settings(speculative_engine_enabled=False))
        cache.get = AsyncMock(return_value=json.dumps(branches))
        result = await buf.get_speculative_response("guild-001", "I attack")
        assert result is None

    @pytest.mark.asyncio
    async def test_corrupt_cache_returns_none(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(return_value="not-valid-json{{{{")
        result = await buf.get_speculative_response("guild-001", "I attack")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_deleted_on_hit(self):
        branches = [
            {"label": "press_advantage", "narrative_text": "You press.", "ambient_audio_key": "combat_tension"},
        ]
        buf, cache = _make_buffer(settings=_make_settings(speculative_similarity_threshold=0.20))
        cache.get = AsyncMock(return_value=json.dumps(branches))
        await buf.get_speculative_response("guild-001", "I attack and swing")
        cache.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_deleted_on_miss(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(return_value=None)
        await buf.get_speculative_response("guild-001", "I attack")
        cache.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_read_error_returns_none(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(side_effect=RuntimeError("Redis unavailable"))
        result = await buf.get_speculative_response("guild-001", "I attack")
        assert result is None

    @pytest.mark.asyncio
    async def test_result_contains_expected_fields(self):
        branches = [
            {"label": "press_advantage", "narrative_text": "Vivid prose here.", "ambient_audio_key": "combat_tension"},
        ]
        buf, cache = _make_buffer(settings=_make_settings(speculative_similarity_threshold=0.20))
        cache.get = AsyncMock(return_value=json.dumps(branches))
        result = await buf.get_speculative_response("guild-001", "I attack swing hit")
        assert result is not None
        assert "label" in result
        assert "narrative_text" in result
        assert "ambient_audio_key" in result
        assert result["narrative_text"] == "Vivid prose here."
        assert result["ambient_audio_key"] == "combat_tension"


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Legacy Prefetch API (backward compat)
# ─────────────────────────────────────────────────────────────────────────────

class TestLegacyPrefetchAPI:
    @pytest.mark.asyncio
    async def test_get_prefetched_text_reads_correct_key(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(return_value="Some atmospheric text.")
        result = await buf.get_prefetched_text("intent-001")
        assert result == "Some atmospheric text."
        cache.get.assert_called_once_with("ironclad:prophet:intent-001:text")

    @pytest.mark.asyncio
    async def test_get_prefetched_audio_reads_correct_key(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(return_value="combat_tension")
        result = await buf.get_prefetched_audio("intent-001")
        assert result == "combat_tension"
        cache.get.assert_called_once_with("ironclad:prophet:intent-001:audio")

    @pytest.mark.asyncio
    async def test_get_prefetched_text_returns_none_on_error(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(side_effect=RuntimeError("Redis error"))
        assert await buf.get_prefetched_text("intent-001") is None

    @pytest.mark.asyncio
    async def test_get_prefetched_audio_returns_none_on_error(self):
        buf, cache = _make_buffer()
        cache.get = AsyncMock(side_effect=RuntimeError("Redis error"))
        assert await buf.get_prefetched_audio("intent-001") is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Enqueue (non-blocking, backpressure)
# ─────────────────────────────────────────────────────────────────────────────

class TestEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_increments_queue(self):
        buf, _ = _make_buffer()
        await buf.enqueue(_make_pipeline_result())
        assert buf._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_drops_silently_when_full(self):
        buf, _ = _make_buffer()
        for _ in range(200):
            await buf.enqueue(_make_pipeline_result())
        # Must not raise — queue bounded at 64
        assert buf._queue.qsize() <= 64


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Ambient Audio Prediction
# ─────────────────────────────────────────────────────────────────────────────

class TestAmbientPrediction:
    def test_combat_branches_map_to_combat_tension(self):
        for label in ["press_advantage", "escape_attempt", "flee", "defensive_action", "emergency_response"]:
            assert _AMBIENT_PREDICTION.get(label) == "combat_tension", f"Expected combat_tension for {label}"

    def test_social_maps_to_tavern_chatter(self):
        assert _AMBIENT_PREDICTION.get("social_interaction") == "tavern_chatter"

    def test_rest_branches_map_to_campfire_quiet(self):
        assert _AMBIENT_PREDICTION.get("recover") == "campfire_quiet"
        assert _AMBIENT_PREDICTION.get("regroup") == "campfire_quiet"


# ─────────────────────────────────────────────────────────────────────────────
# Tests — Full _prefetch integration
# ─────────────────────────────────────────────────────────────────────────────

class TestPrefetchIntegration:
    @pytest.mark.asyncio
    async def test_prefetch_stores_branches_for_guild(self):
        settings = _make_settings(
            speculative_branches=3,
            speculative_ttl_seconds=300,
            speculative_engine_enabled=True,
        )
        storyteller = MagicMock()
        storyteller.generate = AsyncMock(return_value="Evocative atmospheric prose.")
        buf, cache = _make_buffer(storyteller=storyteller, settings=settings)

        with patch.object(buf, "_effective_branch_count", return_value=3):
            await buf._prefetch(_make_pipeline_result(outcome=ActionOutcome.SUCCESS, guild_id="test-guild"))

        speculative_calls = [
            c for c in cache.set.call_args_list
            if "ironclad:speculative:test-guild" in str(c)
        ]
        assert len(speculative_calls) >= 1

    @pytest.mark.asyncio
    async def test_prefetch_disabled_engine_skips_generation(self):
        buf, cache = _make_buffer(settings=_make_settings(speculative_engine_enabled=False))
        await buf._prefetch(_make_pipeline_result())
        cache.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefetch_storyteller_timeout_is_nonfatal(self):
        settings = _make_settings(speculative_branches=1, speculative_engine_enabled=True)
        storyteller = MagicMock()
        storyteller.generate = AsyncMock(side_effect=TimeoutError())
        buf, cache = _make_buffer(storyteller=storyteller, settings=settings)
        with patch.object(buf, "_effective_branch_count", return_value=1):
            # Must not raise
            await buf._prefetch(_make_pipeline_result())

    @pytest.mark.asyncio
    async def test_prefetch_writes_legacy_keys(self):
        settings = _make_settings(speculative_branches=1, speculative_engine_enabled=True)
        storyteller = MagicMock()
        storyteller.generate = AsyncMock(return_value="Atmospheric prose.")
        buf, cache = _make_buffer(storyteller=storyteller, settings=settings)
        with patch.object(buf, "_effective_branch_count", return_value=1):
            await buf._prefetch(_make_pipeline_result(
                outcome=ActionOutcome.SUCCESS, intent_id="intent-xyz"
            ))
        legacy_text_calls = [
            c for c in cache.set.call_args_list
            if "ironclad:prophet:intent-xyz:text" in str(c)
        ]
        assert len(legacy_text_calls) >= 1

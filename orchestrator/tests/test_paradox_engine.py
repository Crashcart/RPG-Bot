"""
Unit tests for orchestrator.services.paradox_engine.ParadoxEngine.

Covers every public API path and all four injection tiers:
  - Passthrough contract (level 1 + out-of-range clamping)
  - Subtle tier     (levels 2–3): uncertainty hedges injected
  - Moderate tier   (levels 4–6): self-correction splices
  - Heavy tier      (levels 7–9): reality-glitch block insertions
  - Maximum tier    (level  10): full narrator breakdown
  - Tier boundaries: no crash on any integer level 1–10
  - Helper: _split_sentences edge cases
  - Edge cases: empty string, single sentence, no punctuation
"""

from __future__ import annotations

import pytest

from orchestrator.services.paradox_engine import (
    ParadoxEngine,
    _HEAVY_GLITCHES,
    _MAX_BREAKDOWN_PREFIX,
    _MAX_BREAKDOWN_SUFFIX,
    _MODERATE_CORRECTIONS,
    _SUBTLE_INSERTS,
    _split_sentences,
)


# ── Fixtures & sample text ──────────────────────────────────────────────────

SIMPLE_NARRATIVE = (
    "You step into the torch-lit corridor. "
    "The stone walls drip with moisture. "
    "A distant sound echoes from the darkness ahead. "
    "Your sword hand tightens on the grip. "
    "Something moves in the shadows."
)

PARAGRAPH_NARRATIVE = (
    "You enter the tavern and survey the room.\n\n"
    "The barkeep eyes you with suspicion. "
    "Three mercenaries sit by the fire.\n\n"
    "A hooded figure in the corner does not look up."
)


@pytest.fixture
def engine() -> ParadoxEngine:
    return ParadoxEngine()


# ── _split_sentences helper ───────────────────────────────────────────────


class TestSplitSentences:
    def test_period_splits(self):
        parts = _split_sentences("First. Second. Third.")
        assert len(parts) == 3

    def test_exclamation_and_question_marks(self):
        parts = _split_sentences("Run! Is it real? Yes.")
        assert len(parts) == 3

    def test_empty_string_returns_empty_list(self):
        assert _split_sentences("") == []

    def test_single_sentence_no_punctuation(self):
        parts = _split_sentences("No punctuation here")
        assert parts == ["No punctuation here"]

    def test_no_empty_parts_in_output(self):
        parts = _split_sentences("One.  Two.  Three.")
        assert all(p.strip() for p in parts)

    def test_five_sentence_narrative(self):
        parts = _split_sentences(SIMPLE_NARRATIVE)
        assert len(parts) == 5


# ── Level 1 passthrough ─────────────────────────────────────────────────────


class TestPassthrough:
    def test_level_1_returns_identical_string(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 1)
        assert result is SIMPLE_NARRATIVE or result == SIMPLE_NARRATIVE

    def test_level_0_clamped_to_1_passthrough(self, engine):
        assert engine.apply(SIMPLE_NARRATIVE, 0) == SIMPLE_NARRATIVE

    def test_negative_level_clamped_to_1(self, engine):
        assert engine.apply(SIMPLE_NARRATIVE, -99) == SIMPLE_NARRATIVE

    def test_level_1_empty_string_passthrough(self, engine):
        assert engine.apply("", 1) == ""


# ── Subtle tier (levels 2–3) ─────────────────────────────────────────────


class TestSubtle:
    @pytest.mark.parametrize("level", [2, 3])
    def test_modifies_narrative(self, engine, level):
        result = engine.apply(SIMPLE_NARRATIVE, level)
        assert result != SIMPLE_NARRATIVE

    @pytest.mark.parametrize("level", [2, 3])
    def test_returns_longer_string(self, engine, level):
        result = engine.apply(SIMPLE_NARRATIVE, level)
        assert len(result) > len(SIMPLE_NARRATIVE)

    def test_hedge_from_known_list_present(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 2)
        assert any(hedge.strip() in result for hedge in _SUBTLE_INSERTS)

    def test_single_sentence_returns_string(self, engine):
        result = engine.apply("Just one sentence.", 2)
        assert isinstance(result, str)

    def test_empty_string_returns_string(self, engine):
        result = engine.apply("", 2)
        assert isinstance(result, str)


# ── Moderate tier (levels 4–6) ──────────────────────────────────────────


class TestModerate:
    @pytest.mark.parametrize("level", [4, 5, 6])
    def test_modifies_narrative(self, engine, level):
        result = engine.apply(SIMPLE_NARRATIVE, level)
        assert result != SIMPLE_NARRATIVE

    def test_correction_marker_from_known_list(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 4)
        assert any(c.strip() in result for c in _MODERATE_CORRECTIONS)

    def test_short_text_falls_back_gracefully(self, engine):
        result = engine.apply("Short.", 4)
        assert isinstance(result, str)

    @pytest.mark.parametrize("level", [4, 5, 6])
    def test_empty_string_no_crash(self, engine, level):
        assert isinstance(engine.apply("", level), str)


# ── Heavy tier (levels 7–9) ───────────────────────────────────────────────


class TestHeavy:
    @pytest.mark.parametrize("level", [7, 8, 9])
    def test_modifies_narrative(self, engine, level):
        result = engine.apply(PARAGRAPH_NARRATIVE, level)
        assert result != PARAGRAPH_NARRATIVE

    def test_glitch_block_from_known_list(self, engine):
        result = engine.apply(PARAGRAPH_NARRATIVE, 7)
        assert any(glitch.strip() in result for glitch in _HEAVY_GLITCHES)

    def test_flat_text_split_at_midpoint(self, engine):
        flat = "No paragraph breaks here just a long wall of text that the engine must split."
        result = engine.apply(flat, 7)
        assert isinstance(result, str)
        assert len(result) > len(flat)

    @pytest.mark.parametrize("level", [7, 8, 9])
    def test_empty_string_no_crash(self, engine, level):
        assert isinstance(engine.apply("", level), str)


# ── Maximum tier (level 10) ──────────────────────────────────────────────


class TestMaximum:
    def test_level_10_modifies_narrative(self, engine):
        assert engine.apply(SIMPLE_NARRATIVE, 10) != SIMPLE_NARRATIVE

    def test_level_11_clamped_to_10(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 11)
        assert _MAX_BREAKDOWN_PREFIX in result

    def test_contains_breakdown_prefix(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 10)
        assert _MAX_BREAKDOWN_PREFIX in result

    def test_contains_transmission_ends_marker(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 10)
        assert "[transmission ends]" in result

    def test_contains_checkpoint_marker(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 10)
        assert "last stable checkpoint" in result

    def test_short_text_no_crash(self, engine):
        result = engine.apply("Short.", 10)
        assert _MAX_BREAKDOWN_PREFIX in result

    def test_empty_string_no_crash(self, engine):
        result = engine.apply("", 10)
        assert isinstance(result, str)

    def test_output_longer_than_input(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 10)
        assert len(result) > len(SIMPLE_NARRATIVE)

    def test_suffix_appended(self, engine):
        result = engine.apply(SIMPLE_NARRATIVE, 10)
        assert "[resuming from last stable checkpoint]" in result


# ── Tier boundary transitions ──────────────────────────────────────────────


class TestTierBoundaries:
    @pytest.mark.parametrize("level", range(1, 11))
    def test_all_levels_return_str(self, engine, level):
        result = engine.apply(SIMPLE_NARRATIVE, level)
        assert isinstance(result, str)

    @pytest.mark.parametrize("level", range(2, 11))
    def test_all_active_levels_non_empty_on_non_empty_input(self, engine, level):
        result = engine.apply(SIMPLE_NARRATIVE, level)
        assert result

    def test_level_3_to_4_no_crash(self, engine):
        engine.apply(SIMPLE_NARRATIVE, 3)
        engine.apply(SIMPLE_NARRATIVE, 4)

    def test_level_6_to_7_no_crash(self, engine):
        engine.apply(SIMPLE_NARRATIVE, 6)
        engine.apply(SIMPLE_NARRATIVE, 7)

    def test_level_9_to_10_no_crash(self, engine):
        engine.apply(SIMPLE_NARRATIVE, 9)
        engine.apply(SIMPLE_NARRATIVE, 10)

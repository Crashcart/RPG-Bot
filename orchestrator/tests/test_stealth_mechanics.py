"""Unit tests for stealth mechanics resolution and deterministic action classification (Issue #11).

Runs against pure Python — no DB, Redis, or Ollama required.
Run with: pytest orchestrator/tests/test_stealth_mechanics.py -v
"""

import pytest

from orchestrator.pipeline.ingestion import _classify_action_category
from orchestrator.schemas.payloads import (
    ActionCategory,
    ActionOutcome,
    DiceRequest,
    MechanicalTruth,
    OllamaResolutionPayload,
    StateDelta,
)


# ─────────────────────────────────────────────────────────────────────────────
# _classify_action_category
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyActionCategory:
    """Keyword-based intent classifier must map free-form input deterministically."""

    def test_empty_input_returns_unknown(self):
        assert _classify_action_category("") == ActionCategory.UNKNOWN

    def test_whitespace_only_returns_unknown(self):
        assert _classify_action_category("   ") == ActionCategory.UNKNOWN

    def test_unrecognised_input_returns_unknown(self):
        assert _classify_action_category("I wait and do nothing") == ActionCategory.UNKNOWN

    # Stealth
    @pytest.mark.parametrize("text", [
        "I hide behind the crates",
        "I sneak past the sleeping guard",
        "I try to move silently through the corridor",
        "I skulk in the shadows",
        "I want to stay hidden",
        "I attempt to avoid detection",
        "I blend in with the crowd",
        "I creep along the wall",
    ])
    def test_stealth_keywords(self, text):
        assert _classify_action_category(text) == ActionCategory.STEALTH, (
            f"Expected STEALTH for: {text!r}"
        )

    def test_stealth_beats_combat_when_both_present(self):
        """Stealth must be classified first — ambush is still a stealth action."""
        assert _classify_action_category("I sneak up and attack the guard") == ActionCategory.STEALTH

    # Combat
    @pytest.mark.parametrize("text", [
        "I swing my broadsword at the goblin",
        "I cast fireball at the cluster of enemies",
        "I shoot the bandit with my crossbow",
        "I punch the guard in the face",
        "I slash at the troll with my axe",
    ])
    def test_combat_keywords(self, text):
        assert _classify_action_category(text) == ActionCategory.COMBAT, (
            f"Expected COMBAT for: {text!r}"
        )

    # Skill check
    @pytest.mark.parametrize("text", [
        "I try to pick the lock",
        "I climb the crumbling wall",
        "I investigate the crime scene",
        "I swim across the river",
    ])
    def test_skill_check_keywords(self, text):
        assert _classify_action_category(text) == ActionCategory.SKILL_CHECK, (
            f"Expected SKILL_CHECK for: {text!r}"
        )

    # Social
    @pytest.mark.parametrize("text", [
        "I talk to the innkeeper",
        "I ask the wizard where the dungeon is",
        "I try to convince the mayor",
        "I greet the merchant at the stall",
    ])
    def test_social_keywords(self, text):
        assert _classify_action_category(text) == ActionCategory.SOCIAL, (
            f"Expected SOCIAL for: {text!r}"
        )

    # Exploration
    @pytest.mark.parametrize("text", [
        "I explore the ruins",
        "I look around the room",
        "I examine the strange lock",
        "I read the ancient tome on the table",
    ])
    def test_exploration_keywords(self, text):
        assert _classify_action_category(text) == ActionCategory.EXPLORATION, (
            f"Expected EXPLORATION for: {text!r}"
        )

    def test_case_insensitive_stealth(self):
        assert _classify_action_category("I SNEAK past the guard") == ActionCategory.STEALTH

    def test_case_insensitive_combat(self):
        assert _classify_action_category("I ATTACK the goblin with my sword") == ActionCategory.COMBAT

    def test_multi_word_stealth_keyword(self):
        assert _classify_action_category("I move silently down the hallway") == ActionCategory.STEALTH


# ─────────────────────────────────────────────────────────────────────────────
# ActionCategory enum
# ─────────────────────────────────────────────────────────────────────────────

class TestActionCategoryEnum:
    """String enum values must match the TDR-defined category identifiers."""

    def test_string_values(self):
        assert ActionCategory.COMBAT       == "combat"
        assert ActionCategory.STEALTH      == "stealth"
        assert ActionCategory.SKILL_CHECK  == "skill_check"
        assert ActionCategory.SAVING_THROW == "saving_throw"
        assert ActionCategory.SOCIAL       == "social"
        assert ActionCategory.EXPLORATION  == "exploration"
        assert ActionCategory.UNKNOWN      == "unknown"

    def test_roundtrip_from_string(self):
        for cat in ActionCategory:
            assert ActionCategory(cat.value) == cat

    def test_total_count(self):
        """Guard against accidental addition/removal of categories."""
        assert len(ActionCategory) == 7


# ─────────────────────────────────────────────────────────────────────────────
# OllamaResolutionPayload — stealth fields
# ─────────────────────────────────────────────────────────────────────────────

def _make_resolution(**overrides) -> OllamaResolutionPayload:
    defaults = dict(
        intent_id="intent-abc",
        action_type="stealth_move",
        action_category=ActionCategory.STEALTH,
        difficulty=14,
        dice_request=DiceRequest(notation="1d20", modifier=3, purpose="Stealth check vs DC 14"),
        roll_result=17,
        outcome=ActionOutcome.SUCCESS,
        state_delta=StateDelta(character_id="char-001"),
    )
    defaults.update(overrides)
    return OllamaResolutionPayload(**defaults)


class TestOllamaResolutionPayloadStealth:

    def test_is_detected_defaults_false(self):
        """Characters are hidden by default — only a failed roll marks them detected."""
        res = _make_resolution()
        assert res.is_detected is False

    def test_is_detected_can_be_true(self):
        res = _make_resolution(is_detected=True)
        assert res.is_detected is True

    def test_action_category_stored_correctly(self):
        res = _make_resolution(action_category=ActionCategory.STEALTH)
        assert res.action_category == ActionCategory.STEALTH

    def test_non_stealth_category_stored(self):
        res = _make_resolution(action_category=ActionCategory.COMBAT)
        assert res.action_category == ActionCategory.COMBAT

    def test_category_fallback_from_string(self):
        """Verify the enum accepts string values (from LLM JSON output)."""
        cat = ActionCategory("stealth")
        assert cat == ActionCategory.STEALTH

    def test_is_detected_false_means_character_hidden(self):
        """
        Contract: is_detected=False + action_category=STEALTH means
        the character successfully avoided detection this turn.
        The narrator must not reveal what NPCs perceive.
        """
        res = _make_resolution(is_detected=False, action_category=ActionCategory.STEALTH)
        assert not res.is_detected
        assert res.action_category == ActionCategory.STEALTH

    def test_is_detected_true_means_character_spotted(self):
        """
        Contract: is_detected=True means the stealth check failed;
        the character is spotted and loses the hidden condition.
        """
        res = _make_resolution(
            is_detected=True,
            outcome=ActionOutcome.FAILURE,
            roll_result=9,
        )
        assert res.is_detected is True
        assert res.outcome == ActionOutcome.FAILURE


# ─────────────────────────────────────────────────────────────────────────────
# MechanicalTruth — is_hidden field
# ─────────────────────────────────────────────────────────────────────────────

def _make_truth(**overrides) -> MechanicalTruth:
    defaults = dict(
        action_type="stealth_move",
        action_category=ActionCategory.STEALTH,
        difficulty=14,
        dice_notation="1d20",
        roll_result=17,
        outcome=ActionOutcome.SUCCESS,
        stat_changes=[],
        status_change=None,
        rulebook_citations=["Core Rules p.177 — Stealth"],
    )
    defaults.update(overrides)
    return MechanicalTruth(**defaults)


class TestMechanicalTruthIsHidden:

    def test_default_is_hidden_false(self):
        truth = _make_truth()
        assert truth.is_hidden is False

    def test_is_hidden_true(self):
        truth = _make_truth(is_hidden=True)
        assert truth.is_hidden is True

    def test_non_stealth_action_is_hidden_defaults_false(self):
        truth = _make_truth(action_category=ActionCategory.COMBAT)
        assert truth.is_hidden is False

    def test_is_hidden_narration_contract(self):
        """
        Document the narration contract enforced by the system prompt:
          is_hidden=True  → narrator narrates ONLY the character's own senses.
          is_hidden=False → narrator may describe the full scene normally.
        """
        hidden = _make_truth(is_hidden=True)
        visible = _make_truth(is_hidden=False)
        assert hidden.is_hidden is not visible.is_hidden

    def test_action_category_on_truth(self):
        truth = _make_truth(action_category=ActionCategory.STEALTH)
        assert truth.action_category == ActionCategory.STEALTH

    def test_truth_serialises_is_hidden(self):
        """MechanicalTruth must serialise is_hidden so it appears in the prompt JSON."""
        truth = _make_truth(is_hidden=True)
        d = truth.model_dump()
        assert "is_hidden" in d
        assert d["is_hidden"] is True

    def test_truth_serialises_action_category(self):
        truth = _make_truth()
        d = truth.model_dump()
        assert "action_category" in d
        assert d["action_category"] == ActionCategory.STEALTH

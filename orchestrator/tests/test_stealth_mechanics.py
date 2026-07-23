"""
Tests for Stealth Mechanics Resolution & Deterministic Dice Orchestration.

Covers:
  - _classify_action_category() — pure Python keyword router
  - ActionCategory enum contract
  - OllamaResolutionPayload stealth fields
  - MechanicalTruth stealth fields
  - Narration contract: is_hidden defaults and semantics
"""

from __future__ import annotations

import pytest

from orchestrator.pipeline.ingestion import _classify_action_category
from orchestrator.schemas.payloads import (
    ActionCategory,
    ActionOutcome,
    CharacterStatus,
    DiceRequest,
    MechanicalTruth,
    OllamaResolutionPayload,
    StateDelta,
)


# ─────────────────────────────────────────────────────────────────────────────
# ActionCategory Enum Contract
# ─────────────────────────────────────────────────────────────────────────────

class TestActionCategoryEnum:
    def test_string_values(self):
        assert ActionCategory.STEALTH.value     == "stealth"
        assert ActionCategory.COMBAT.value      == "combat"
        assert ActionCategory.SKILL_CHECK.value == "skill_check"
        assert ActionCategory.SAVING_THROW.value == "saving_throw"
        assert ActionCategory.SOCIAL.value      == "social"
        assert ActionCategory.EXPLORATION.value == "exploration"
        assert ActionCategory.UNKNOWN.value     == "unknown"

    def test_roundtrip_from_string(self):
        for cat in ActionCategory:
            assert ActionCategory(cat.value) == cat

    def test_count_guard(self):
        assert len(ActionCategory) == 7, (
            "A category was added/removed — update stealth guardrails and tests."
        )


# ─────────────────────────────────────────────────────────────────────────────
# _classify_action_category() — keyword router
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw_input,expected", [
    # Stealth — highest priority
    ("I sneak past the guard quietly",          ActionCategory.STEALTH),
    ("I hide behind the crates",               ActionCategory.STEALTH),
    ("Creeping through the shadows",           ActionCategory.STEALTH),
    ("I stalk the merchant silently",          ActionCategory.STEALTH),
    ("Blend into the crowd and go undetected", ActionCategory.STEALTH),

    # Stealth beats combat when both appear
    ("I sneak up and stab the goblin",         ActionCategory.STEALTH),
    ("Silently draw my sword and attack",      ActionCategory.STEALTH),

    # Combat
    ("I swing my broadsword at the orc",       ActionCategory.COMBAT),
    ("Attack the goblin to my left",           ActionCategory.COMBAT),
    ("I cast fireball at the undead horde",    ActionCategory.COMBAT),
    ("Shoot the bandit with my crossbow",      ActionCategory.COMBAT),

    # Saving throw (multi-word phrase)
    ("I resist the poison with a saving throw", ActionCategory.SAVING_THROW),
    ("Fortitude check to endure the cold",     ActionCategory.SAVING_THROW),

    # Skill check
    ("I pick the lock on the vault door",      ActionCategory.SKILL_CHECK),
    ("Perception check on the far wall",       ActionCategory.SKILL_CHECK),
    ("I investigate the crime scene",          ActionCategory.SKILL_CHECK),
    ("Climb the cliff face",                   ActionCategory.SKILL_CHECK),

    # Social
    ("I try to persuade the innkeeper",        ActionCategory.SOCIAL),
    ("Negotiate with the merchant for a deal", ActionCategory.SOCIAL),
    ("Talk to the wizard about the prophecy",  ActionCategory.SOCIAL),

    # Exploration
    ("I walk towards the village",             ActionCategory.EXPLORATION),
    ("Open the door and look inside",          ActionCategory.EXPLORATION),
    ("I rest by the campfire",                 ActionCategory.EXPLORATION),

    # Unknown — no recognisable keyword
    ("",                                       ActionCategory.UNKNOWN),
    ("Hmm",                                    ActionCategory.UNKNOWN),
])
class TestClassifyActionCategory:
    def test_classification(self, raw_input: str, expected: ActionCategory):
        result = _classify_action_category(raw_input)
        assert result == expected, (
            f"Input: {raw_input!r}\n"
            f"Expected: {expected}\n"
            f"Got:      {result}"
        )


class TestClassifyActionCategoryEdgeCases:
    def test_case_insensitive(self):
        assert _classify_action_category("SNEAK PAST THE GUARD") == ActionCategory.STEALTH
        assert _classify_action_category("ATTACK THE GOBLIN")    == ActionCategory.COMBAT

    def test_mixed_case(self):
        assert _classify_action_category("Sneaking Through shadows") == ActionCategory.STEALTH

    def test_multi_word_stealth_keyword(self):
        assert _classify_action_category("I want to go undetected") == ActionCategory.STEALTH

    def test_stealth_priority_over_exploration(self):
        assert _classify_action_category("I tiptoe through the open door") == ActionCategory.STEALTH

    def test_returns_action_category_type(self):
        result = _classify_action_category("I attack the skeleton")
        assert isinstance(result, ActionCategory)


# ─────────────────────────────────────────────────────────────────────────────
# OllamaResolutionPayload — stealth fields
# ─────────────────────────────────────────────────────────────────────────────

def _make_resolution(**overrides) -> OllamaResolutionPayload:
    defaults = dict(
        intent_id="test-intent-id",
        action_type="stealth_move",
        difficulty=15,
        dice_request=DiceRequest(notation="1d20", modifier=3, purpose="stealth check"),
        roll_result=18,
        outcome=ActionOutcome.SUCCESS,
        state_delta=StateDelta(character_id="char-001"),
    )
    defaults.update(overrides)
    return OllamaResolutionPayload(**defaults)


class TestOllamaResolutionPayloadStealth:
    def test_is_detected_defaults_false(self):
        r = _make_resolution()
        assert r.is_detected is False

    def test_action_category_defaults_unknown(self):
        r = _make_resolution()
        assert r.action_category == ActionCategory.UNKNOWN

    def test_stealth_hidden_contract(self):
        r = _make_resolution(
            action_category=ActionCategory.STEALTH,
            is_detected=False,
        )
        assert r.action_category == ActionCategory.STEALTH
        assert r.is_detected is False

    def test_stealth_spotted_contract(self):
        r = _make_resolution(
            action_category=ActionCategory.STEALTH,
            is_detected=True,
        )
        assert r.is_detected is True

    def test_non_stealth_is_detected_is_false(self):
        r = _make_resolution(
            action_category=ActionCategory.COMBAT,
            is_detected=False,
        )
        assert r.is_detected is False

    def test_serialisation_roundtrip(self):
        r = _make_resolution(
            action_category=ActionCategory.STEALTH,
            is_detected=True,
        )
        data = r.model_dump()
        assert data["action_category"] == "stealth"
        assert data["is_detected"] is True
        restored = OllamaResolutionPayload(**data)
        assert restored.action_category == ActionCategory.STEALTH
        assert restored.is_detected is True


# ─────────────────────────────────────────────────────────────────────────────
# MechanicalTruth — stealth narration contract
# ─────────────────────────────────────────────────────────────────────────────

def _make_truth(**overrides) -> MechanicalTruth:
    defaults = dict(
        action_type="stealth_move",
        difficulty=15,
        dice_notation="1d20",
        roll_result=18,
        outcome=ActionOutcome.SUCCESS,
        stat_changes=[],
        status_change=None,
        rulebook_citations=[],
    )
    defaults.update(overrides)
    return MechanicalTruth(**defaults)


class TestMechanicalTruthStealth:
    def test_is_hidden_defaults_false(self):
        t = _make_truth()
        assert t.is_hidden is False

    def test_action_category_defaults_unknown(self):
        t = _make_truth()
        assert t.action_category == ActionCategory.UNKNOWN

    def test_hidden_narration_contract(self):
        t = _make_truth(action_category=ActionCategory.STEALTH, is_hidden=True)
        assert t.is_hidden is True
        assert t.action_category == ActionCategory.STEALTH

    def test_detected_narration_contract(self):
        t = _make_truth(action_category=ActionCategory.STEALTH, is_hidden=False)
        assert t.is_hidden is False

    def test_non_stealth_is_hidden_ignored(self):
        t = _make_truth(action_category=ActionCategory.COMBAT, is_hidden=False)
        assert t.action_category == ActionCategory.COMBAT
        assert t.is_hidden is False

    def test_serialisation_includes_stealth_fields(self):
        t = _make_truth(action_category=ActionCategory.STEALTH, is_hidden=True)
        data = t.model_dump()
        assert "action_category" in data
        assert "is_hidden" in data
        assert data["action_category"] == "stealth"
        assert data["is_hidden"] is True

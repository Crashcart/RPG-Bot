"""
Unit tests for orchestrator/prompts/immersion_prompts.py

Tests cover:
  - is_combat_action()
  - is_combat_end()
  - detect_channel_directive()
  - build_thread_content()
  - AMBIENT_AUDIO_MAP content
  - WHISPER_SYSTEM_PROMPT / WHISPER_PROMPT constants
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.prompts.immersion_prompts import (
    AMBIENT_AUDIO_MAP,
    WHISPER_PROMPT,
    WHISPER_SYSTEM_PROMPT,
    build_thread_content,
    detect_channel_directive,
    is_combat_action,
    is_combat_end,
)
from orchestrator.schemas.payloads import (
    ActionOutcome,
    CharacterStatus,
    DiceRequest,
    OllamaResolutionPayload,
    StateDelta,
    StatDelta,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_resolution(
    action_type: str = "melee_attack",
    difficulty: int = 12,
    notation: str = "1d20",
    modifier: int = 2,
    roll_result: int = 15,
    outcome: ActionOutcome = ActionOutcome.SUCCESS,
    stat_deltas=None,
    status_change=None,
    inventory_delta=None,
    citations=None,
    reasoning: str = "Attack connects.",
    intent_id: str = "intent-001",
) -> OllamaResolutionPayload:
    return OllamaResolutionPayload(
        intent_id=intent_id,
        action_type=action_type,
        difficulty=difficulty,
        dice_request=DiceRequest(notation=notation, modifier=modifier, purpose="attack roll"),
        roll_result=roll_result,
        outcome=outcome,
        state_delta=StateDelta(
            character_id="char-001",
            stat_deltas=stat_deltas or [],
            status_change=status_change,
            inventory_delta=inventory_delta or [],
        ),
        rulebook_citations=citations or [],
        reasoning=reasoning,
    )


# ─────────────────────────────────────────────────────────────────────────────
# is_combat_action()
# ─────────────────────────────────────────────────────────────────────────────

class TestIsCombatAction:
    def test_melee_attack_is_combat(self):
        assert is_combat_action("melee_attack") is True

    def test_ranged_is_combat(self):
        assert is_combat_action("ranged_shot") is True

    def test_shoot_is_combat(self):
        assert is_combat_action("shoot_crossbow") is True

    def test_stab_is_combat(self):
        assert is_combat_action("stab_enemy") is True

    def test_slash_is_combat(self):
        assert is_combat_action("slash_attack") is True

    def test_strike_is_combat(self):
        assert is_combat_action("power_strike") is True

    def test_dodge_is_combat(self):
        assert is_combat_action("dodge_roll") is True

    def test_parry_is_combat(self):
        assert is_combat_action("parry_blow") is True

    def test_grapple_is_combat(self):
        assert is_combat_action("grapple_opponent") is True

    def test_charge_is_combat(self):
        assert is_combat_action("charge") is True

    def test_ambush_is_combat(self):
        assert is_combat_action("ambush_guard") is True

    def test_combat_keyword_is_combat(self):
        assert is_combat_action("combat_maneuver") is True

    def test_throw_is_combat(self):
        assert is_combat_action("throw_dagger") is True

    def test_suppress_is_combat(self):
        assert is_combat_action("suppress_fire") is True

    def test_flank_is_combat(self):
        assert is_combat_action("flank_enemy") is True

    def test_draw_weapon_is_combat(self):
        assert is_combat_action("draw_weapon") is True

    def test_reload_is_combat(self):
        assert is_combat_action("reload_gun") is True

    def test_social_interaction_not_combat(self):
        assert is_combat_action("social_interaction") is False

    def test_exploration_not_combat(self):
        assert is_combat_action("exploration") is False

    def test_craft_not_combat(self):
        assert is_combat_action("craft_potion") is False

    def test_skill_check_not_combat(self):
        assert is_combat_action("skill_check_stealth") is False

    def test_case_insensitive(self):
        assert is_combat_action("MELEE_ATTACK") is True
        assert is_combat_action("Ranged_Shot") is True


# ─────────────────────────────────────────────────────────────────────────────
# is_combat_end()
# ─────────────────────────────────────────────────────────────────────────────

class TestIsCombatEnd:
    def test_dead_status_ends_combat(self):
        assert is_combat_end("melee_attack", "", CharacterStatus.DEAD) is True

    def test_flee_action_ends_combat(self):
        assert is_combat_end("flee", "", None) is True

    def test_escape_action_ends_combat(self):
        assert is_combat_end("escape", "", None) is True

    def test_surrender_action_ends_combat(self):
        assert is_combat_end("surrender", "", None) is True

    def test_retreat_action_ends_combat(self):
        assert is_combat_end("retreat", "", None) is True

    def test_yield_action_ends_combat(self):
        assert is_combat_end("yield_fight", "", None) is True

    def test_combat_end_action_ends_combat(self):
        assert is_combat_end("combat_end", "", None) is True

    def test_disengage_action_ends_combat(self):
        assert is_combat_end("disengage", "", None) is True

    def test_fall_unconscious_action_ends_combat(self):
        assert is_combat_end("fall_unconscious", "", None) is True

    def test_death_save_action_ends_combat(self):
        assert is_combat_end("death_save", "", None) is True

    def test_retreat_in_reasoning_ends_combat(self):
        assert is_combat_end("exploration", "The enemy chose to retreat from battle.", None) is True

    def test_escape_in_reasoning_ends_combat(self):
        assert is_combat_end("skill_check", "Player managed to escape the ambush.", None) is True

    def test_normal_attack_does_not_end_combat(self):
        assert is_combat_end("melee_attack", "Attack connects for 8 damage.", None) is False

    def test_social_action_does_not_end_combat(self):
        assert is_combat_end("social_interaction", "Negotiation attempt.", None) is False

    def test_alive_status_does_not_end_combat(self):
        assert is_combat_end("melee_attack", "", CharacterStatus.ALIVE) is False

    def test_case_insensitive_action(self):
        assert is_combat_end("FLEE", "", None) is True

    def test_none_reasoning_does_not_crash(self):
        assert is_combat_end("melee_attack", None, None) is False


# ─────────────────────────────────────────────────────────────────────────────
# detect_channel_directive()
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectChannelDirective:
    def test_captured_triggers_prison(self):
        action, key = detect_channel_directive(
            "skill_check", "The player was captured by the guards.", "failure"
        )
        assert action == "move_to"
        assert key in ("dungeon", "prison")

    def test_imprisoned_triggers_prison(self):
        action, key = detect_channel_directive(
            "social", "Player was imprisoned for the crime.", "failure"
        )
        assert action == "move_to"
        assert key in ("dungeon", "prison")

    def test_dungeon_keyword_triggers_dungeon(self):
        action, key = detect_channel_directive(
            "combat", "Player was thrown into the dungeon and chained.", "failure"
        )
        assert action == "move_to"
        assert key == "dungeon"

    def test_cell_keyword_triggers_dungeon(self):
        action, key = detect_channel_directive(
            "skill_check", "Player was locked in a cell underground.", "failure"
        )
        assert action == "move_to"
        assert key == "dungeon"

    def test_pit_keyword_triggers_dungeon(self):
        action, key = detect_channel_directive(
            "combat", "Player was thrown into the pit and chained.", "failure"
        )
        assert action == "move_to"
        assert key == "dungeon"

    def test_escaped_triggers_restore(self):
        action, key = detect_channel_directive(
            "skill_check", "Player escaped through the window.", "success"
        )
        assert action == "restore"
        assert key == "main"

    def test_freed_triggers_restore(self):
        action, key = detect_channel_directive(
            "npc_interaction", "Player was freed by the rebel faction.", "success"
        )
        assert action == "restore"
        assert key == "main"

    def test_breaks_free_triggers_restore(self):
        action, key = detect_channel_directive(
            "strength_check", "Player breaks free of the shackles.", "success"
        )
        assert action == "restore"
        assert key == "main"

    def test_escape_checked_before_capture(self):
        # Both keywords present — escape should win
        action, key = detect_channel_directive(
            "action", "Player escaped even though arrested earlier.", "success"
        )
        assert action == "restore"

    def test_no_keywords_returns_none_none(self):
        action, key = detect_channel_directive(
            "melee_attack", "Attack connects for 8 damage.", "success"
        )
        assert action is None
        assert key is None

    def test_none_reasoning_returns_none_none(self):
        action, key = detect_channel_directive("melee_attack", None, "success")
        assert action is None
        assert key is None

    def test_empty_reasoning_returns_none_none(self):
        action, key = detect_channel_directive("skill_check", "", "success")
        assert action is None
        assert key is None


# ─────────────────────────────────────────────────────────────────────────────
# build_thread_content()
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildThreadContent:
    def _basic_resolution(self, **kw) -> OllamaResolutionPayload:
        return _make_resolution(**kw)

    def test_action_type_formatted_in_output(self):
        res = self._basic_resolution(action_type="melee_attack")
        result = build_thread_content(res, "Kira")
        assert "Melee Attack" in result

    def test_roll_result_in_output(self):
        res = self._basic_resolution(roll_result=17)
        result = build_thread_content(res, "Kira")
        assert "17" in result

    def test_difficulty_in_output(self):
        res = self._basic_resolution(difficulty=14)
        result = build_thread_content(res, "Kira")
        assert "14" in result

    def test_character_name_signed_at_end(self):
        res = self._basic_resolution()
        result = build_thread_content(res, "BraveHero")
        assert "BraveHero" in result

    def test_outcome_value_in_output(self):
        res = self._basic_resolution(outcome=ActionOutcome.CRITICAL_SUCCESS)
        result = build_thread_content(res, "Kira")
        assert "CRITICAL SUCCESS" in result.upper()

    def test_stat_deltas_shown(self):
        deltas = [StatDelta(stat_key="hp", old_value=20, new_value=12)]
        res = self._basic_resolution(stat_deltas=deltas)
        result = build_thread_content(res, "Kira")
        assert "hp" in result
        assert "20" in result
        assert "12" in result

    def test_numeric_delta_sign_shown(self):
        deltas = [StatDelta(stat_key="stamina", old_value=10, new_value=14)]
        res = self._basic_resolution(stat_deltas=deltas)
        result = build_thread_content(res, "Kira")
        assert "+4" in result

    def test_negative_delta_shown(self):
        deltas = [StatDelta(stat_key="hp", old_value=20, new_value=5)]
        res = self._basic_resolution(stat_deltas=deltas)
        result = build_thread_content(res, "Kira")
        assert "-15" in result

    def test_inventory_delta_shown_when_present(self):
        inv = [{"name": "Health Potion", "quantity": -1}]
        res = self._basic_resolution(inventory_delta=inv)
        result = build_thread_content(res, "Kira")
        assert "Health Potion" in result

    def test_status_change_shown_when_dead(self):
        res = self._basic_resolution(status_change=CharacterStatus.DEAD)
        result = build_thread_content(res, "Kira")
        assert "DEAD" in result.upper()

    def test_rulebook_citations_shown(self):
        res = self._basic_resolution(citations=["PHB p.194 – Melee Attack"])
        result = build_thread_content(res, "Kira")
        assert "PHB p.194" in result

    def test_reasoning_shown_truncated(self):
        long_reasoning = "X" * 400
        res = self._basic_resolution(reasoning=long_reasoning)
        result = build_thread_content(res, "Kira")
        assert "X" * 280 in result

    def test_no_stat_deltas_no_stat_section(self):
        res = self._basic_resolution(stat_deltas=[])
        result = build_thread_content(res, "Kira")
        assert "Stat Changes" not in result

    def test_critical_failure_emoji_present(self):
        res = self._basic_resolution(outcome=ActionOutcome.CRITICAL_FAILURE)
        result = build_thread_content(res, "Kira")
        assert "💀" in result

    def test_success_emoji_present(self):
        res = self._basic_resolution(outcome=ActionOutcome.SUCCESS)
        result = build_thread_content(res, "Kira")
        assert "✅" in result

    def test_failure_emoji_present(self):
        res = self._basic_resolution(outcome=ActionOutcome.FAILURE)
        result = build_thread_content(res, "Kira")
        assert "❌" in result

    def test_partial_success_emoji_present(self):
        res = self._basic_resolution(outcome=ActionOutcome.PARTIAL_SUCCESS)
        result = build_thread_content(res, "Kira")
        assert "⚡" in result

    def test_critical_success_emoji_present(self):
        res = self._basic_resolution(outcome=ActionOutcome.CRITICAL_SUCCESS)
        result = build_thread_content(res, "Kira")
        assert "🌟" in result

    def test_max_three_citations_shown(self):
        res = self._basic_resolution(citations=[f"Cite {i}" for i in range(6)])
        result = build_thread_content(res, "Kira")
        assert "Cite 0" in result
        assert "Cite 2" in result
        assert "Cite 3" not in result


# ─────────────────────────────────────────────────────────────────────────────
# AMBIENT_AUDIO_MAP
# ─────────────────────────────────────────────────────────────────────────────

class TestAmbientAudioMap:
    def test_combat_maps_to_combat_tension(self):
        assert AMBIENT_AUDIO_MAP["combat encounter"] == "combat_tension"

    def test_social_maps_to_tavern_chatter(self):
        assert AMBIENT_AUDIO_MAP["social interaction"] == "tavern_chatter"

    def test_exploration_maps_to_dungeon_ambience(self):
        assert AMBIENT_AUDIO_MAP["exploration/stealth"] == "dungeon_ambience"

    def test_crafting_maps_to_workshop_sounds(self):
        assert AMBIENT_AUDIO_MAP["crafting/downtime"] == "workshop_sounds"

    def test_general_scene_maps_to_none(self):
        assert AMBIENT_AUDIO_MAP["general scene"] is None

    def test_all_values_are_str_or_none(self):
        for val in AMBIENT_AUDIO_MAP.values():
            assert val is None or isinstance(val, str)


# ─────────────────────────────────────────────────────────────────────────────
# WHISPER_SYSTEM_PROMPT / WHISPER_PROMPT constants
# ─────────────────────────────────────────────────────────────────────────────

class TestWhisperPromptConstants:
    def test_whisper_system_prompt_has_sentence_limit(self):
        assert "2-3 sentences" in WHISPER_SYSTEM_PROMPT

    def test_whisper_system_prompt_forbids_game_terms(self):
        lower = WHISPER_SYSTEM_PROMPT.lower()
        assert "game-mechanical" in lower or "game mechanic" in lower

    def test_whisper_system_prompt_second_person(self):
        assert "second person" in WHISPER_SYSTEM_PROMPT.lower() or "You notice" in WHISPER_SYSTEM_PROMPT

    def test_whisper_prompt_renders(self):
        result = WHISPER_PROMPT.format(
            narrative_summary="The guard escorted you to the gates.",
            npc_list="Captain Aldric (suspicious), Town Guard (nervous)",
            mechanical_outcome="partial_success — guard is wary but did not attack",
        )
        assert "Aldric" in result
        assert "partial_success" in result

"""
Unit tests for orchestrator/prompts/gm_prompts.py

Tests cover:
  - STRUCTURAL_PATTERNS regex list (should match / should not match)
  - BRAND_BLOCKLIST content assertions
  - SUBAGENT_PROMPT_TEMPLATES registry completeness
  - GM_PLANNING_PROMPT template rendering
  - GM_SYNTHESIS_PROMPT template rendering
  - Per-task SUBAGENT_*_PROMPT template rendering
  - MUSIC_SCENE_PROMPTS coverage
  - GM_DIRECTIVE_BLOCK / GM_STAT_CHANGE_BLOCK rendering
"""

from __future__ import annotations

import re

import pytest

from orchestrator.prompts.gm_prompts import (
    BRAND_BLOCKLIST,
    GM_DIRECTIVE_BLOCK,
    GM_PLANNING_PROMPT,
    GM_STAT_CHANGE_BLOCK,
    GM_SYNTHESIS_PROMPT,
    GM_SYSTEM_PROMPT,
    MUSIC_SCENE_PROMPTS,
    STRUCTURAL_PATTERNS,
    SUBAGENT_COMBAT_FLAVOUR_PROMPT,
    SUBAGENT_ENVIRONMENT_PROMPT,
    SUBAGENT_ITEM_DESCRIPTION_PROMPT,
    SUBAGENT_NPC_DIALOGUE_PROMPT,
    SUBAGENT_PROMPT_TEMPLATES,
    SUBAGENT_SCENE_DESCRIBER_PROMPT,
    SUBAGENT_SOUND_DIRECTOR_PROMPT,
    SUBAGENT_SYSTEM_PROMPT,
)


# ─────────────────────────────────────────────────────────────────────────────
# GM_SYSTEM_PROMPT integrity
# ─────────────────────────────────────────────────────────────────────────────

class TestGMSystemPrompt:
    def test_immersion_rules_present(self):
        assert "ABSOLUTE IMMERSION RULES" in GM_SYSTEM_PROMPT

    def test_anti_railroading_present(self):
        assert "ANTI-RAILROADING MANDATE" in GM_SYSTEM_PROMPT

    def test_zero_fourth_wall_breaks(self):
        assert "FOURTH-WALL" in GM_SYSTEM_PROMPT

    def test_zero_real_world_brands(self):
        assert "REAL-WORLD BRANDS" in GM_SYSTEM_PROMPT

    def test_player_agency_lock(self):
        assert "PLAYER AGENCY LOCK" in GM_SYSTEM_PROMPT

    def test_is_non_empty_string(self):
        assert isinstance(GM_SYSTEM_PROMPT, str)
        assert len(GM_SYSTEM_PROMPT) > 200


# ─────────────────────────────────────────────────────────────────────────────
# STRUCTURAL_PATTERNS — patterns that SHOULD match (structural text)
# ─────────────────────────────────────────────────────────────────────────────

class TestStructuralPatternsMatch:
    def _any_match(self, text: str) -> bool:
        return any(p.search(text) for p in STRUCTURAL_PATTERNS)

    def test_matches_markdown_h2(self):
        assert self._any_match("## Scene Description")

    def test_matches_markdown_h1(self):
        assert self._any_match("# Chapter One")

    def test_matches_chapter_heading(self):
        assert self._any_match("Chapter 3 The Dark Forest")

    def test_matches_section_heading(self):
        assert self._any_match("Section 2 Overview")

    def test_matches_part_heading(self):
        assert self._any_match("Part 1 Introduction")

    def test_matches_equals_divider(self):
        assert self._any_match("=== Scene Break ===")

    def test_matches_dash_divider(self):
        assert self._any_match("---")

    def test_matches_asterisk_divider(self):
        assert self._any_match("***")

    def test_matches_numbered_list_item(self):
        assert self._any_match("1. The guard steps forward")

    def test_matches_dash_bullet(self):
        assert self._any_match("- The door creaks open")

    def test_matches_asterisk_bullet(self):
        assert self._any_match("* The torch flickers")

    def test_matches_bullet_point_char(self):
        assert self._any_match("• An arrow whistles past")


class TestStructuralPatternsNoMatch:
    """Ensure prose that looks slightly structural does NOT match."""

    def _any_match(self, text: str) -> bool:
        return any(p.search(text) for p in STRUCTURAL_PATTERNS)

    def test_no_match_plain_prose(self):
        assert not self._any_match(
            "You step into the shadow of the mountain and feel the cold air on your face."
        )

    def test_no_match_sentence_with_number(self):
        assert not self._any_match("You have 3 torches left in your pack.")

    def test_no_match_npc_dialogue(self):
        assert not self._any_match('"Get out of my tavern," the barkeep growls.')


# ─────────────────────────────────────────────────────────────────────────────
# BRAND_BLOCKLIST content
# ─────────────────────────────────────────────────────────────────────────────

class TestBrandBlocklist:
    def test_is_list(self):
        assert isinstance(BRAND_BLOCKLIST, list)

    def test_non_empty(self):
        assert len(BRAND_BLOCKLIST) >= 50

    def test_contains_tech_brands(self):
        lower = [b.lower() for b in BRAND_BLOCKLIST]
        for brand in ("google", "amazon", "microsoft", "apple"):
            assert brand in lower, f"Expected '{brand}' in blocklist"

    def test_contains_social_media(self):
        lower = [b.lower() for b in BRAND_BLOCKLIST]
        for brand in ("facebook", "twitter", "instagram"):
            assert brand in lower, f"Expected '{brand}' in blocklist"

    def test_contains_fast_food(self):
        lower = [b.lower() for b in BRAND_BLOCKLIST]
        assert any("mcdonald" in b for b in lower)

    def test_contains_real_world_ip(self):
        lower = [b.lower() for b in BRAND_BLOCKLIST]
        assert any("star wars" in b for b in lower)

    def test_all_entries_are_strings(self):
        assert all(isinstance(b, str) for b in BRAND_BLOCKLIST)

    def test_no_empty_strings(self):
        assert all(len(b.strip()) > 0 for b in BRAND_BLOCKLIST)


# ─────────────────────────────────────────────────────────────────────────────
# SUBAGENT_PROMPT_TEMPLATES registry
# ─────────────────────────────────────────────────────────────────────────────

class TestSubagentPromptTemplates:
    REQUIRED_TASK_TYPES = {
        "npc_dialogue",
        "environmental_description",
        "combat_flavour",
        "item_description",
        "sound_director",
        "scene_describer",
    }

    def test_all_required_types_present(self):
        for task_type in self.REQUIRED_TASK_TYPES:
            assert task_type in SUBAGENT_PROMPT_TEMPLATES, (
                f"Missing task type: {task_type}"
            )

    def test_values_are_non_empty_strings(self):
        for key, tmpl in SUBAGENT_PROMPT_TEMPLATES.items():
            assert isinstance(tmpl, str) and len(tmpl) > 0, (
                f"Template for '{key}' is empty"
            )

    def test_npc_dialogue_maps_to_correct_template(self):
        assert SUBAGENT_PROMPT_TEMPLATES["npc_dialogue"] is SUBAGENT_NPC_DIALOGUE_PROMPT

    def test_environmental_description_maps_to_correct_template(self):
        assert SUBAGENT_PROMPT_TEMPLATES["environmental_description"] is SUBAGENT_ENVIRONMENT_PROMPT

    def test_combat_flavour_maps_to_correct_template(self):
        assert SUBAGENT_PROMPT_TEMPLATES["combat_flavour"] is SUBAGENT_COMBAT_FLAVOUR_PROMPT

    def test_item_description_maps_to_correct_template(self):
        assert SUBAGENT_PROMPT_TEMPLATES["item_description"] is SUBAGENT_ITEM_DESCRIPTION_PROMPT

    def test_sound_director_maps_to_correct_template(self):
        assert SUBAGENT_PROMPT_TEMPLATES["sound_director"] is SUBAGENT_SOUND_DIRECTOR_PROMPT

    def test_scene_describer_maps_to_correct_template(self):
        assert SUBAGENT_PROMPT_TEMPLATES["scene_describer"] is SUBAGENT_SCENE_DESCRIBER_PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# SUBAGENT_SYSTEM_PROMPT
# ─────────────────────────────────────────────────────────────────────────────

class TestSubagentSystemPrompt:
    def test_uncensored_mode_declared(self):
        assert "UNCENSORED" in SUBAGENT_SYSTEM_PROMPT

    def test_no_brand_names_rule(self):
        assert "brand" in SUBAGENT_SYSTEM_PROMPT.lower()

    def test_no_structural_formatting_rule(self):
        assert "structural formatting" in SUBAGENT_SYSTEM_PROMPT.lower()

    def test_is_string(self):
        assert isinstance(SUBAGENT_SYSTEM_PROMPT, str)


# ─────────────────────────────────────────────────────────────────────────────
# Per-task subagent prompt rendering
# ─────────────────────────────────────────────────────────────────────────────

class TestSubagentNpcDialoguePrompt:
    def _render(self, **kw):
        defaults = dict(
            entity_name="Grimshaw",
            entity_role="corrupt city guard",
            scene_context="A dark alley at midnight, rain falling.",
            player_action_context="Player accused the guard of taking bribes.",
            tone="menacing",
            max_words=80,
        )
        defaults.update(kw)
        return SUBAGENT_NPC_DIALOGUE_PROMPT.format(**defaults)

    def test_entity_name_injected(self):
        assert "Grimshaw" in self._render()

    def test_entity_role_injected(self):
        assert "corrupt city guard" in self._render()

    def test_tone_injected(self):
        assert "menacing" in self._render()

    def test_word_limit_injected(self):
        assert "80" in self._render()

    def test_player_action_context_injected(self):
        assert "bribes" in self._render()


class TestSubagentEnvironmentPrompt:
    def _render(self, **kw):
        defaults = dict(
            entity_name="The Whispering Vault",
            entity_role="ancient subterranean crypt",
            scene_context="First entry into the dungeon.",
            player_action_context="Player opened the heavy stone door.",
            tone="fearful",
            max_words=100,
        )
        defaults.update(kw)
        return SUBAGENT_ENVIRONMENT_PROMPT.format(**defaults)

    def test_location_injected(self):
        assert "Whispering Vault" in self._render()

    def test_tone_injected(self):
        assert "fearful" in self._render()

    def test_catalyst_injected(self):
        assert "stone door" in self._render()


class TestSubagentCombatFlavourPrompt:
    def _render(self, **kw):
        defaults = dict(
            entity_name="Serrated Longsword",
            entity_role="character's primary weapon",
            scene_context="Melee combat against a bandit chief.",
            player_action_context="Player declared an overhead strike.",
            tone="gritty",
            max_words=60,
        )
        defaults.update(kw)
        return SUBAGENT_COMBAT_FLAVOUR_PROMPT.format(**defaults)

    def test_weapon_name_injected(self):
        assert "Serrated Longsword" in self._render()

    def test_tone_injected(self):
        assert "gritty" in self._render()


class TestSubagentItemDescriptionPrompt:
    def _render(self, **kw):
        defaults = dict(
            entity_name="Obsidian Amulet",
            entity_role="magical focus artifact",
            scene_context="Player searches a fallen wizard's body.",
            player_action_context="Player picks up the amulet for close inspection.",
            tone="reverent",
            max_words=70,
        )
        defaults.update(kw)
        return SUBAGENT_ITEM_DESCRIPTION_PROMPT.format(**defaults)

    def test_item_name_injected(self):
        assert "Obsidian Amulet" in self._render()

    def test_discovery_context_injected(self):
        assert "inspection" in self._render()


class TestSubagentSoundDirectorPrompt:
    def _render(self, **kw):
        defaults = dict(
            scene_context="Player enters a burning building.",
            player_action_context="Kicks down the door.",
            tone="tense",
        )
        defaults.update(kw)
        return SUBAGENT_SOUND_DIRECTOR_PROMPT.format(**defaults)

    def test_json_array_instruction_present(self):
        result = self._render()
        assert "JSON" in result

    def test_max_sfx_rule_present(self):
        result = self._render()
        assert "3" in result

    def test_scene_context_injected(self):
        assert "burning building" in self._render()


class TestSubagentSceneDescriberPrompt:
    def _render(self, **kw):
        defaults = dict(
            scene_context="A sunlit market square in a fantasy city.",
            player_action_context="Player looks around after emerging from the sewers.",
            tone="humorous",
        )
        defaults.update(kw)
        return SUBAGENT_SCENE_DESCRIBER_PROMPT.format(**defaults)

    def test_image_prompt_instruction_present(self):
        result = self._render()
        assert "image" in result.lower()

    def test_scene_context_injected(self):
        assert "market square" in self._render()

    def test_no_brand_rule_present(self):
        assert "brand" in self._render().lower()


# ─────────────────────────────────────────────────────────────────────────────
# GM_PLANNING_PROMPT template rendering
# ─────────────────────────────────────────────────────────────────────────────

class TestGMPlanningPromptRendering:
    def _render(self):
        return GM_PLANNING_PROMPT.format(
            player_action="I search the bookshelves for a hidden lever.",
            mechanical_outcome="partial_success — found a clue but triggered a trap",
            npc_list="None present",
            environment_type="study/library",
        )

    def test_player_action_present(self):
        assert "hidden lever" in self._render()

    def test_mechanical_outcome_present(self):
        assert "partial_success" in self._render()

    def test_environment_type_present(self):
        assert "study/library" in self._render()

    def test_task_type_options_documented(self):
        result = self._render()
        for tt in ("npc_dialogue", "environmental_description", "combat_flavour", "item_description"):
            assert tt in result

    def test_tone_options_documented(self):
        result = self._render()
        for tone in ("gritty", "menacing", "humorous", "reverent"):
            assert tone in result


# ─────────────────────────────────────────────────────────────────────────────
# GM_SYNTHESIS_PROMPT template rendering
# ─────────────────────────────────────────────────────────────────────────────

class TestGMSynthesisPromptRendering:
    def _render(self, directive_block="", stat_change_block=""):
        return GM_SYNTHESIS_PROMPT.format(
            directive_block=directive_block,
            mechanical_context='{"outcome":"success"}',
            story_context="The keep has been besieged for three days.",
            player_action="I charge through the gates.",
            assembled_elements="NPC shouts: 'The gates are breached!'",
            direct_elements="The portcullis falls behind you.",
            stat_change_block=stat_change_block,
        )

    def test_mechanical_context_injected(self):
        assert "success" in self._render()

    def test_story_context_injected(self):
        assert "besieged" in self._render()

    def test_player_action_injected(self):
        assert "charge through" in self._render()

    def test_assembled_elements_injected(self):
        assert "gates are breached" in self._render()

    def test_direct_elements_injected(self):
        assert "portcullis" in self._render()

    def test_anti_railroading_reminder_in_synthesis(self):
        assert "Anti-railroading" in self._render() or "anti-railroading" in self._render()


# ─────────────────────────────────────────────────────────────────────────────
# GM_DIRECTIVE_BLOCK rendering
# ─────────────────────────────────────────────────────────────────────────────

class TestGMDirectiveBlock:
    def test_directive_text_injected(self):
        directive = "Have the innkeeper reveal the spy's identity during small talk."
        result = GM_DIRECTIVE_BLOCK.format(directives=directive)
        assert directive in result

    def test_highest_priority_label_present(self):
        result = GM_DIRECTIVE_BLOCK.format(directives="test directive")
        assert "HIGHEST PRIORITY" in result

    def test_world_architect_label_present(self):
        result = GM_DIRECTIVE_BLOCK.format(directives="test")
        assert "WORLD ARCHITECT" in result


# ─────────────────────────────────────────────────────────────────────────────
# GM_STAT_CHANGE_BLOCK rendering
# ─────────────────────────────────────────────────────────────────────────────

class TestGMStatChangeBlock:
    def test_changes_injected(self):
        changes = "HP: 24 → 16 (lost 8 from goblin strike)\nGold: 10 → 7"
        result = GM_STAT_CHANGE_BLOCK.format(changes=changes)
        assert "HP: 24 → 16" in result
        assert "Gold: 10 → 7" in result

    def test_mandatory_inclusion_label_present(self):
        result = GM_STAT_CHANGE_BLOCK.format(changes="HP: 10 → 5")
        assert "MANDATORY INCLUSION" in result


# ─────────────────────────────────────────────────────────────────────────────
# MUSIC_SCENE_PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

class TestMusicScenePrompts:
    REQUIRED_SCENES = {"combat", "exploration", "social", "tension", "rest"}

    def test_all_required_scenes_present(self):
        for scene in self.REQUIRED_SCENES:
            assert scene in MUSIC_SCENE_PROMPTS, f"Missing scene: {scene}"

    def test_values_are_non_empty_strings(self):
        for key, val in MUSIC_SCENE_PROMPTS.items():
            assert isinstance(val, str) and len(val) > 0, f"Empty prompt for '{key}'"

    def test_combat_mentions_tempo(self):
        assert "bpm" in MUSIC_SCENE_PROMPTS["combat"].lower()

    def test_exploration_mentions_wonder(self):
        assert "discovery" in MUSIC_SCENE_PROMPTS["exploration"].lower() or \
               "wonder" in MUSIC_SCENE_PROMPTS["exploration"].lower()

    def test_tension_mentions_suspense(self):
        assert "suspense" in MUSIC_SCENE_PROMPTS["tension"].lower() or \
               "tense" in MUSIC_SCENE_PROMPTS["tension"].lower()

    def test_rest_mentions_peaceful(self):
        assert "peaceful" in MUSIC_SCENE_PROMPTS["rest"].lower() or \
               "calm" in MUSIC_SCENE_PROMPTS["rest"].lower() or \
               "soft" in MUSIC_SCENE_PROMPTS["rest"].lower()

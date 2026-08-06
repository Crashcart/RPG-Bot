"""
Unit tests for orchestrator/prompts/guardrails.py

Tests cover:
  - build_narrative_system_prompt (Gemini path)
  - build_local_narrative_prompt  (local Ollama path)
  - MECHANICAL_SYSTEM_PROMPT constant integrity
"""

from __future__ import annotations

import pytest

from orchestrator.prompts.guardrails import (
    MECHANICAL_SYSTEM_PROMPT,
    build_local_narrative_prompt,
    build_narrative_system_prompt,
)


# ─────────────────────────────────────────────────────────────────────────────
# MECHANICAL_SYSTEM_PROMPT constant
# ─────────────────────────────────────────────────────────────────────────────

class TestMechanicalSystemPrompt:
    def test_contains_anti_sycophancy_lock(self):
        assert "ANTI-SYCOPHANCY LOCK" in MECHANICAL_SYSTEM_PROMPT

    def test_contains_dice_supremacy_lock(self):
        assert "DICE SUPREMACY LOCK" in MECHANICAL_SYSTEM_PROMPT

    def test_contains_narrative_prohibition(self):
        assert "NARRATIVE PROHIBITION" in MECHANICAL_SYSTEM_PROMPT

    def test_contains_json_output_format(self):
        assert "OUTPUT FORMAT" in MECHANICAL_SYSTEM_PROMPT
        assert "OllamaResolutionPayload" in MECHANICAL_SYSTEM_PROMPT

    def test_contains_vehicle_rules(self):
        assert "VEHICLE RULES" in MECHANICAL_SYSTEM_PROMPT
        assert "vehicle_deltas" in MECHANICAL_SYSTEM_PROMPT

    def test_is_non_empty_string(self):
        assert isinstance(MECHANICAL_SYSTEM_PROMPT, str)
        assert len(MECHANICAL_SYSTEM_PROMPT) > 500


# ─────────────────────────────────────────────────────────────────────────────
# build_narrative_system_prompt (Gemini / cloud path)
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildNarrativeSystemPrompt:
    def _build(self, system="D&D 5e", truth='{"outcome":"success"}', lines=None, tone=""):
        return build_narrative_system_prompt(
            system=system,
            mechanical_truth_json=truth,
            story_context_lines=lines,
            world_tone_block=tone,
        )

    def test_system_name_injected(self):
        result = self._build(system="Mothership")
        assert "Mothership" in result

    def test_mechanical_truth_injected(self):
        truth = '{"outcome":"critical_failure","roll_result":2}'
        result = self._build(truth=truth)
        assert truth in result

    def test_no_story_context_uses_opening_scene_placeholder(self):
        result = self._build(lines=None)
        assert "opening scene" in result.lower()

    def test_story_context_lines_injected_as_bullets(self):
        lines = ["The tavern burned down last session", "Aldric the merchant is dead"]
        result = self._build(lines=lines)
        assert "• The tavern burned down last session" in result
        assert "• Aldric the merchant is dead" in result

    def test_empty_story_context_list_uses_placeholder(self):
        result = self._build(lines=[])
        assert "opening scene" in result.lower()

    def test_world_tone_block_prepended(self):
        tone = "GENRE: Grimdark pirate horror. Tone is bleak and unrelenting."
        result = self._build(tone=tone)
        assert result.startswith(tone)

    def test_world_tone_block_empty_not_prepended(self):
        result = self._build(tone="")
        assert not result.startswith("\n")
        assert "ROLE:" in result[:50]

    def test_mechanical_truth_lock_present(self):
        result = self._build()
        assert "MECHANICAL TRUTH LOCK" in result

    def test_story_continuity_lock_present(self):
        result = self._build()
        assert "STORY CONTINUITY LOCK" in result

    def test_anti_sycophancy_lock_present(self):
        result = self._build()
        assert "ANTI-SYCOPHANCY LOCK" in result

    def test_lethal_outcome_protocol_present(self):
        result = self._build()
        assert "LETHAL OUTCOME PROTOCOL" in result

    def test_anti_railroading_mandate_present(self):
        result = self._build()
        assert "ANTI-RAILROADING MANDATE" in result

    def test_returns_string(self):
        assert isinstance(self._build(), str)

    def test_tone_and_context_combined(self):
        tone = "SETTING: Space western."
        lines = ["The ship is named The Vagrant Star"]
        result = self._build(tone=tone, lines=lines)
        assert result.startswith(tone)
        assert "The ship is named The Vagrant Star" in result

    def test_multiple_story_lines_all_present(self):
        lines = [f"Fact {i}" for i in range(5)]
        result = self._build(lines=lines)
        for line in lines:
            assert line in result


# ─────────────────────────────────────────────────────────────────────────────
# build_local_narrative_prompt (local Ollama / uncensored path)
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildLocalNarrativePrompt:
    def _build(self, system="Call of Cthulhu", truth='{"outcome":"failure"}', lines=None, tone=""):
        return build_local_narrative_prompt(
            system=system,
            mechanical_truth_json=truth,
            story_context_lines=lines,
            world_tone_block=tone,
        )

    def test_system_name_injected(self):
        assert "Call of Cthulhu" in self._build()

    def test_mechanical_truth_injected(self):
        truth = '{"roll_result":3}'
        assert truth in self._build(truth=truth)

    def test_uncensored_mode_present(self):
        result = self._build()
        assert "UNCENSORED" in result.upper()

    def test_no_story_context_uses_placeholder(self):
        result = self._build(lines=None)
        assert "opening scene" in result.lower()

    def test_story_context_lines_become_bullets(self):
        lines = ["The lighthouse keeper vanished", "Strange lights at sea"]
        result = self._build(lines=lines)
        assert "• The lighthouse keeper vanished" in result
        assert "• Strange lights at sea" in result

    def test_world_tone_prepended(self):
        tone = "COSMIC HORROR: reality frays at every turn."
        result = self._build(tone=tone)
        assert result.startswith(tone)

    def test_no_tone_result_starts_with_role(self):
        result = self._build(tone="")
        assert "ROLE:" in result[:60]

    def test_mechanical_truth_lock_present(self):
        assert "MECHANICAL TRUTH LOCK" in self._build()

    def test_anti_railroading_present(self):
        assert "ANTI-RAILROADING MANDATE" in self._build()

    def test_returns_string(self):
        assert isinstance(self._build(), str)

    def test_local_and_gemini_differ_by_uncensored(self):
        truth = '{"x":1}'
        local = build_local_narrative_prompt("D&D 5e", truth, None, "")
        gemini = build_narrative_system_prompt("D&D 5e", truth, None, "")
        # local has uncensored grant; gemini does not
        assert "UNCENSORED" in local.upper()
        # both share core constraints
        assert "MECHANICAL TRUTH LOCK" in local
        assert "MECHANICAL TRUTH LOCK" in gemini

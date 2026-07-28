"""Unit tests for GMDirector — Tier 1 Central Storyteller."""

from __future__ import annotations

import asyncio
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.services.gm_director import (
    GMDirector,
    _build_directive_block,
    _build_stat_change_block,
    _build_thread_event,
    _build_tts_cues,
    _extract_environment_type,
    _extract_npc_list,
    _format_assembled_elements,
    _format_mechanical_context,
    _infer_ambient_audio_key,
    _parse_json_safely,
    _parse_sfx_cues,
    _resolve_scene_type,
    _strip_structural_text,
)
from orchestrator.schemas.payloads import (
    ActionOutcome,
    CharacterSnapshot,
    CharacterStatus,
    DiceRequest,
    DirectiveType,
    GMDirective,
    GMPlanResult,
    NarrativeResponsePayload,
    OllamaResolutionPayload,
    StateCommitPayload,
    StateDelta,
    StatDelta,
    SubAgentResult,
    SubAgentTask,
    ThreadEvent,
    TTSCue,
)


# ── Shared fixture helpers ────────────────────────────────────────────────────

def _make_resolution(
    action_type: str = "melee_attack",
    outcome: ActionOutcome = ActionOutcome.SUCCESS,
    reasoning: str = "Hit connects.",
    stat_deltas: list | None = None,
    inventory_delta: list | None = None,
    status_change: CharacterStatus | None = None,
    roll_result: int = 15,
    difficulty: int = 12,
) -> OllamaResolutionPayload:
    return OllamaResolutionPayload(
        intent_id="intent-001",
        action_type=action_type,
        difficulty=difficulty,
        dice_request=DiceRequest(notation="1d20", modifier=3, purpose="attack roll"),
        roll_result=roll_result,
        outcome=outcome,
        state_delta=StateDelta(
            character_id="char-001",
            stat_deltas=stat_deltas or [],
            inventory_delta=inventory_delta or [],
            status_change=status_change,
        ),
        reasoning=reasoning,
    )


def _make_sub_result(
    task_type: str = "npc_dialogue",
    entity_name: str = "Guard",
    raw_output: str = "Halt, stranger!",
    voice_id: str = "en-US-GuyNeural",
    node_name: str = "actor-node-01",
    brand_violation: bool = False,
) -> SubAgentResult:
    task = SubAgentTask(
        task_type=task_type,
        entity_name=entity_name,
        entity_role="city guard",
        scene_context="City gate at dusk.",
        player_action_context="Player approached the gate.",
        tone="gritty",
        max_words=80,
    )
    return SubAgentResult(
        task=task,
        raw_output=raw_output,
        node_name=node_name,
        voice_id=voice_id,
        ttft_ms=200,
        brand_violation=brand_violation,
    )


# ── TestParseJsonSafely ────────────────────────────────────────────────────────

class TestParseJsonSafely:
    def test_plain_json_object(self):
        raw = '{"sub_tasks": [], "direct_elements": ["intro"]}'
        result = _parse_json_safely(raw)
        assert result["direct_elements"] == ["intro"]

    def test_json_fenced_with_backticks(self):
        raw = '```json\n{"key": "value"}\n```'
        result = _parse_json_safely(raw)
        assert result["key"] == "value"

    def test_json_fenced_no_language(self):
        raw = '```\n{"key": 42}\n```'
        result = _parse_json_safely(raw)
        assert result["key"] == 42

    def test_json_embedded_in_prose(self):
        raw = 'Here is my plan: {"sub_tasks": []} end of plan.'
        result = _parse_json_safely(raw)
        assert result["sub_tasks"] == []

    def test_raises_on_unparseable_input(self):
        with pytest.raises(ValueError, match="Could not parse JSON"):
            _parse_json_safely("this is not json at all")

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            _parse_json_safely("")

    def test_nested_json_object(self):
        payload = {"sub_tasks": [{"task_type": "npc_dialogue"}], "direct_elements": []}
        result = _parse_json_safely(json.dumps(payload))
        assert result["sub_tasks"][0]["task_type"] == "npc_dialogue"


# ── TestStripStructuralText ───────────────────────────────────────────────────

class TestStripStructuralText:
    def test_clean_prose_unchanged(self):
        prose = "You step into the tavern. Smoke hangs heavy in the air."
        text, count = _strip_structural_text(prose)
        assert count == 0
        assert "You step into the tavern" in text

    def test_markdown_header_stripped(self):
        text = "## Chapter One\nYou step forward."
        cleaned, count = _strip_structural_text(text)
        assert count > 0
        assert "##" not in cleaned

    def test_numbered_list_stripped(self):
        text = "1. First thing happens.\n2. Second thing happens."
        cleaned, count = _strip_structural_text(text)
        assert count > 0

    def test_horizontal_divider_stripped(self):
        text = "Prose before.\n---\nProse after."
        cleaned, count = _strip_structural_text(text)
        assert count > 0
        assert "---" not in cleaned

    def test_multiple_blank_lines_collapsed(self):
        text = "Line one.\n\n\n\nLine two."
        cleaned, _ = _strip_structural_text(text)
        assert "\n\n\n" not in cleaned

    def test_returns_stripped_count(self):
        text = "## Header\n1. Item\n---\nProse."
        _, count = _strip_structural_text(text)
        assert count >= 2


# ── TestBuildStatChangeBlock ───────────────────────────────────────────────────

class TestBuildStatChangeBlock:
    def test_returns_empty_when_nothing_changed(self):
        resolution = _make_resolution()
        result = _build_stat_change_block(resolution)
        assert result == ""

    def test_returns_block_when_stat_delta_present(self):
        resolution = _make_resolution(
            stat_deltas=[StatDelta(stat_key="hp", old_value=20, new_value=12)]
        )
        result = _build_stat_change_block(resolution)
        assert "hp" in result
        assert "20" in result
        assert "12" in result

    def test_returns_block_when_inventory_changed(self):
        resolution = _make_resolution(
            inventory_delta=[{"name": "Healing Potion", "quantity": -1}]
        )
        result = _build_stat_change_block(resolution)
        assert "Healing Potion" in result

    def test_returns_block_when_status_changed(self):
        resolution = _make_resolution(status_change=CharacterStatus.DEAD)
        result = _build_stat_change_block(resolution)
        assert "DEAD" in result

    def test_inventory_qty_format_signed(self):
        resolution = _make_resolution(
            inventory_delta=[{"name": "Arrow", "quantity": -3}]
        )
        result = _build_stat_change_block(resolution)
        assert "Arrow" in result


# ── TestExtractNpcList ─────────────────────────────────────────────────────────

class TestExtractNpcList:
    def test_empty_reasoning_returns_empty(self):
        resolution = _make_resolution(reasoning="")
        assert _extract_npc_list(resolution) == ""

    def test_extracts_proper_nouns(self):
        resolution = _make_resolution(reasoning="Gareth swings first. Mira dodges.")
        result = _extract_npc_list(resolution)
        assert "Gareth" in result
        assert "Mira" in result

    def test_excludes_common_stopwords(self):
        resolution = _make_resolution(reasoning="The player rolls With great effort.")
        result = _extract_npc_list(resolution)
        assert "The" not in result
        assert "With" not in result

    def test_deduplicates_repeated_names(self):
        resolution = _make_resolution(reasoning="Gareth attacks. Gareth retreats.")
        result = _extract_npc_list(resolution)
        assert result.count("Gareth") == 1

    def test_caps_at_five_names(self):
        resolution = _make_resolution(
            reasoning="Alpha Beta Gamma Delta Epsilon Zeta Eta"
        )
        result = _extract_npc_list(resolution)
        names = [n for n in result.split(", ") if n]
        assert len(names) <= 5


# ── TestExtractEnvironmentType ─────────────────────────────────────────────────

class TestExtractEnvironmentType:
    def test_combat_keywords(self):
        assert _extract_environment_type(_make_resolution("melee_attack")) == "combat encounter"
        assert _extract_environment_type(_make_resolution("shoot")) == "combat encounter"

    def test_social_keywords(self):
        assert _extract_environment_type(_make_resolution("persuade")) == "social interaction"
        assert _extract_environment_type(_make_resolution("intimidate")) == "social interaction"

    def test_exploration_keywords(self):
        assert _extract_environment_type(_make_resolution("sneak")) == "exploration/stealth"
        assert _extract_environment_type(_make_resolution("investigate")) == "exploration/stealth"

    def test_crafting_keywords(self):
        assert _extract_environment_type(_make_resolution("craft_armor")) == "crafting/downtime"

    def test_fallback_general(self):
        assert _extract_environment_type(_make_resolution("rest")) == "general scene"


# ── TestFormatAssembledElements ────────────────────────────────────────────────

class TestFormatAssembledElements:
    def test_empty_results_returns_empty_string(self):
        assert _format_assembled_elements([]) == ""

    def test_formats_single_result(self):
        result = _make_sub_result(task_type="npc_dialogue", entity_name="Guard",
                                  raw_output="Halt!")
        output = _format_assembled_elements([result])
        assert "[NPC_DIALOGUE — Guard]" in output
        assert "Halt!" in output

    def test_skips_empty_raw_output(self):
        result = _make_sub_result(raw_output="")
        output = _format_assembled_elements([result])
        assert output == ""

    def test_multiple_results_joined(self):
        r1 = _make_sub_result(task_type="npc_dialogue", entity_name="Guard",
                              raw_output="Stop right there!")
        r2 = _make_sub_result(task_type="environmental_description",
                              entity_name="Dungeon", raw_output="Damp stone walls.")
        output = _format_assembled_elements([r1, r2])
        assert "Guard" in output
        assert "Dungeon" in output
        assert "\n\n" in output


# ── TestFormatMechanicalContext ────────────────────────────────────────────────

class TestFormatMechanicalContext:
    def test_includes_action_type(self):
        resolution = _make_resolution(action_type="skill_check")
        output = _format_mechanical_context(resolution)
        assert "skill_check" in output

    def test_includes_roll_and_dc(self):
        resolution = _make_resolution(roll_result=18, difficulty=14)
        output = _format_mechanical_context(resolution)
        assert "18" in output
        assert "14" in output

    def test_includes_stat_changes(self):
        resolution = _make_resolution(
            stat_deltas=[StatDelta(stat_key="stamina", old_value=10, new_value=7)]
        )
        output = _format_mechanical_context(resolution)
        assert "stamina" in output
        assert "10" in output
        assert "7" in output

    def test_no_changes_placeholder(self):
        resolution = _make_resolution()
        output = _format_mechanical_context(resolution)
        assert "no stat changes" in output
        assert "no inventory changes" in output

    def test_inventory_delta_included(self):
        resolution = _make_resolution(
            inventory_delta=[{"name": "Rope", "quantity": 1}]
        )
        output = _format_mechanical_context(resolution)
        assert "Rope" in output


# ── TestBuildTtsCues ───────────────────────────────────────────────────────────

class TestBuildTtsCues:
    def test_empty_results_returns_empty_list(self):
        assert _build_tts_cues([]) == []

    def test_npc_dialogue_produces_cue(self):
        result = _make_sub_result(
            task_type="npc_dialogue",
            entity_name="Merchant",
            raw_output="Ten gold pieces, friend.",
            voice_id="en-GB-LibbyNeural",
            node_name="actor-node-02",
        )
        cues = _build_tts_cues([result])
        assert len(cues) == 1
        assert cues[0].entity_name == "Merchant"
        assert cues[0].text == "Ten gold pieces, friend."
        assert cues[0].voice_id == "en-GB-LibbyNeural"

    def test_non_dialogue_tasks_skipped(self):
        env_result = _make_sub_result(
            task_type="environmental_description",
            raw_output="Fog rolls in from the sea.",
        )
        cues = _build_tts_cues([env_result])
        assert cues == []

    def test_empty_raw_output_skipped(self):
        result = _make_sub_result(task_type="npc_dialogue", raw_output="")
        cues = _build_tts_cues([result])
        assert cues == []

    def test_multiple_npc_results(self):
        r1 = _make_sub_result(task_type="npc_dialogue", entity_name="Thug",
                              raw_output="Your money or your life!")
        r2 = _make_sub_result(task_type="npc_dialogue", entity_name="Mage",
                              raw_output="I cast fireball!")
        cues = _build_tts_cues([r1, r2])
        assert len(cues) == 2
        assert cues[0].entity_name == "Thug"
        assert cues[1].entity_name == "Mage"


# ── TestBuildThreadEvent ───────────────────────────────────────────────────────

class TestBuildThreadEvent:
    def test_combat_action_returns_combat_event(self):
        resolution = _make_resolution(action_type="melee_attack")
        event, title, content = _build_thread_event(resolution, "Kira")
        assert event == ThreadEvent.COMBAT
        assert "Combat" in title

    def test_non_combat_returns_none(self):
        resolution = _make_resolution(action_type="skill_check")
        event, title, content = _build_thread_event(resolution, "Kira")
        assert event is None
        assert content is None

    def test_dead_status_returns_close(self):
        resolution = _make_resolution(
            action_type="melee_attack",
            status_change=CharacterStatus.DEAD,
        )
        event, title, content = _build_thread_event(resolution, "Kira")
        assert event == ThreadEvent.CLOSE

    def test_flee_action_returns_close(self):
        resolution = _make_resolution(action_type="flee")
        event, title, content = _build_thread_event(resolution, "Kira")
        assert event == ThreadEvent.CLOSE

    def test_combat_content_is_string_when_present(self):
        resolution = _make_resolution(action_type="slash")
        _, _, content = _build_thread_event(resolution, "Kira")
        assert isinstance(content, str)


# ── TestBuildDirectiveBlock ────────────────────────────────────────────────────

class TestBuildDirectiveBlock:
    def test_none_returns_empty_string(self):
        assert _build_directive_block(None) == ""

    def test_empty_list_returns_empty_string(self):
        assert _build_directive_block([]) == ""

    def test_single_directive_formatted(self):
        directive = GMDirective(
            directive_id="dir-001",
            campaign_id="camp-001",
            admin_id="admin-001",
            directive_type=DirectiveType.SCENE_DIRECTIVE,
            directive_text="Make the tavern feel welcoming.",
            priority=5,
            status="pending",
            submitted_at=datetime.now(timezone.utc),
        )
        result = _build_directive_block([directive])
        assert "SCENE DIRECTIVE" in result
        assert "Make the tavern feel welcoming." in result

    def test_multiple_directives_all_included(self):
        directives = [
            GMDirective(
                directive_id=f"dir-{i}",
                campaign_id="camp-001",
                admin_id="admin-001",
                directive_type=DirectiveType.NPC_HINT,
                directive_text=f"Hint {i}",
                priority=5,
                status="pending",
                submitted_at=datetime.now(timezone.utc),
            )
            for i in range(3)
        ]
        result = _build_directive_block(directives)
        assert "Hint 0" in result
        assert "Hint 2" in result


# ── TestInferAmbientAudioKey ───────────────────────────────────────────────────

class TestInferAmbientAudioKey:
    def test_combat_action(self):
        resolution = _make_resolution(action_type="melee_attack")
        key = _infer_ambient_audio_key(resolution)
        assert key == "combat_tension"

    def test_social_action(self):
        resolution = _make_resolution(action_type="persuade")
        key = _infer_ambient_audio_key(resolution)
        assert key == "tavern_chatter"

    def test_stealth_action(self):
        resolution = _make_resolution(action_type="sneak_past")
        key = _infer_ambient_audio_key(resolution)
        assert key == "dungeon_ambience"

    def test_crafting_action(self):
        resolution = _make_resolution(action_type="craft_potion")
        key = _infer_ambient_audio_key(resolution)
        assert key == "workshop_sounds"

    def test_unknown_action_returns_none(self):
        resolution = _make_resolution(action_type="ooc")
        key = _infer_ambient_audio_key(resolution)
        assert key is None


# ── TestParseSfxCues ───────────────────────────────────────────────────────────

class TestParseSfxCues:
    def test_valid_json_array(self):
        raw = '[{"description": "sword_clash", "delay_ms": 0}]'
        cues = _parse_sfx_cues(raw)
        assert len(cues) == 1
        assert cues[0].sfx_key == "sword_clash"
        assert cues[0].delay_ms == 0

    def test_json_fenced(self):
        raw = '```json\n[{"description": "footsteps", "delay_ms": 500}]\n```'
        cues = _parse_sfx_cues(raw)
        assert len(cues) == 1
        assert cues[0].sfx_key == "footsteps"

    def test_missing_description_key_skipped(self):
        raw = '[{"sound": "boom", "delay_ms": 0}]'
        cues = _parse_sfx_cues(raw)
        assert cues == []

    def test_invalid_json_returns_empty(self):
        cues = _parse_sfx_cues("not json at all")
        assert cues == []

    def test_caps_at_three_cues(self):
        items = [{"description": f"sfx_{i}", "delay_ms": 0} for i in range(6)]
        raw = json.dumps(items)
        cues = _parse_sfx_cues(raw)
        assert len(cues) == 3

    def test_delay_ms_defaults_to_zero(self):
        raw = '[{"description": "thunder"}]'
        cues = _parse_sfx_cues(raw)
        assert cues[0].delay_ms == 0


# ── TestResolveSceneType ───────────────────────────────────────────────────────

class TestResolveSceneType:
    def test_combat_action_type(self):
        assert _resolve_scene_type("melee_attack", None) == "combat"
        assert _resolve_scene_type("shoot", None) == "combat"

    def test_social_action_type(self):
        assert _resolve_scene_type("persuade", None) == "social"
        assert _resolve_scene_type("barter", None) == "social"

    def test_exploration_action_type(self):
        assert _resolve_scene_type("sneak", None) == "exploration"
        assert _resolve_scene_type("investigate", None) == "exploration"

    def test_rest_action_type(self):
        assert _resolve_scene_type("rest", None) == "rest"
        assert _resolve_scene_type("craft_tool", None) == "rest"

    def test_tension_from_ambient_key(self):
        assert _resolve_scene_type("ooc", "tension_drone") == "tension"

    def test_unknown_returns_none(self):
        assert _resolve_scene_type("ooc", None) is None
        assert _resolve_scene_type("unknown_action", None) is None


# ── TestGMDirectorSelectStoryteller ───────────────────────────────────────────

@pytest.mark.asyncio
class TestGMDirectorSelectStoryteller:
    def _make_director(self, cloud_provider="gemini", claude=None):
        gemini = AsyncMock()
        node_router = AsyncMock()
        dispatcher = AsyncMock()
        story_memory = AsyncMock()
        director = GMDirector(
            gemini=gemini,
            node_router=node_router,
            dispatcher=dispatcher,
            story_memory=story_memory,
            cloud_provider=cloud_provider,
            claude=claude,
        )
        return director, gemini, node_router

    async def test_cloud_on_gemini_returns_gemini(self):
        director, gemini, node_router = self._make_director()
        node_router.is_storyteller_enabled.return_value = True
        result = await director._select_storyteller()
        assert result is gemini

    async def test_cloud_on_claude_returns_claude(self):
        claude = AsyncMock()
        director, gemini, node_router = self._make_director("claude", claude=claude)
        node_router.is_storyteller_enabled.return_value = True
        result = await director._select_storyteller()
        assert result is claude

    async def test_cloud_off_local_node_available(self):
        director, gemini, node_router = self._make_director()
        node_router.is_storyteller_enabled.return_value = False
        local_node = AsyncMock()
        node_router.get_storyteller_client.return_value = local_node
        result = await director._select_storyteller()
        assert result is local_node

    async def test_cloud_off_no_local_falls_back_to_gemini(self):
        director, gemini, node_router = self._make_director()
        node_router.is_storyteller_enabled.return_value = False
        node_router.get_storyteller_client.return_value = None
        result = await director._select_storyteller()
        assert result is gemini


# ── TestGMDirectorPlanningPass ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGMDirectorPlanningPass:
    def _make_director(self):
        gemini = AsyncMock()
        node_router = AsyncMock()
        dispatcher = AsyncMock()
        story_memory = AsyncMock()
        director = GMDirector(
            gemini=gemini,
            node_router=node_router,
            dispatcher=dispatcher,
            story_memory=story_memory,
        )
        return director

    async def test_valid_json_plan_parsed(self):
        director = self._make_director()
        storyteller = AsyncMock()
        task_dict = {
            "task_type": "npc_dialogue",
            "entity_name": "Guard",
            "entity_role": "city guard",
            "scene_context": "City gate.",
            "player_action_context": "Approaches.",
            "tone": "gritty",
            "max_words": 80,
        }
        storyteller.generate.return_value = json.dumps({
            "sub_tasks": [task_dict],
            "direct_elements": ["gate ambiance"],
        })
        resolution = _make_resolution(reasoning="Guard Gareth blocks the gate.")
        plan = await director._planning_pass(storyteller, resolution, "I walk to the gate.")
        assert len(plan.sub_tasks) == 1
        assert plan.sub_tasks[0].task_type == "npc_dialogue"
        assert "gate ambiance" in plan.direct_elements

    async def test_json_fallback_on_fenced_response(self):
        director = self._make_director()
        storyteller = AsyncMock()
        storyteller.generate.return_value = (
            '```json\n{"sub_tasks": [], "direct_elements": ["forest path"]}\n```'
        )
        resolution = _make_resolution()
        plan = await director._planning_pass(storyteller, resolution, "I walk through the forest.")
        assert plan.direct_elements == ["forest path"]

    async def test_exception_returns_fallback_plan(self):
        director = self._make_director()
        storyteller = AsyncMock()
        storyteller.generate.side_effect = RuntimeError("Network timeout")
        resolution = _make_resolution()
        plan = await director._planning_pass(storyteller, resolution, "I run!")
        assert plan.sub_tasks == []
        assert "full scene" in plan.direct_elements

    async def test_malformed_sub_task_skipped(self):
        director = self._make_director()
        storyteller = AsyncMock()
        storyteller.generate.return_value = json.dumps({
            "sub_tasks": [
                {"task_type": "npc_dialogue", "entity_name": "Guard",
                 "entity_role": "guard", "scene_context": "gate",
                 "player_action_context": "walks", "tone": "gritty", "max_words": 80},
                {"INVALID_FIELD": "bad"},
            ],
            "direct_elements": [],
        })
        resolution = _make_resolution()
        plan = await director._planning_pass(storyteller, resolution, "test")
        assert len(plan.sub_tasks) == 1  # malformed skipped
        assert plan.sub_tasks[0].task_type == "npc_dialogue"


# ── TestGMDirectorGenerateWhisper ──────────────────────────────────────────────

@pytest.mark.asyncio
class TestGMDirectorGenerateWhisper:
    def _make_director(self):
        return GMDirector(
            gemini=AsyncMock(),
            node_router=AsyncMock(),
            dispatcher=AsyncMock(),
            story_memory=AsyncMock(),
        )

    async def test_successful_whisper_returned(self):
        director = self._make_director()
        storyteller = AsyncMock()
        storyteller.generate.return_value = "You notice his left hand trembling."
        resolution = _make_resolution()
        plan = GMPlanResult()
        sub_results = [_make_sub_result(task_type="npc_dialogue", entity_name="Merchant")]
        result = await director._generate_whisper(
            storyteller, resolution, plan, sub_results, "I greet the merchant."
        )
        assert result == "You notice his left hand trembling."

    async def test_exception_returns_none(self):
        director = self._make_director()
        storyteller = AsyncMock()
        storyteller.generate.side_effect = Exception("API error")
        resolution = _make_resolution()
        plan = GMPlanResult()
        sub_results = [_make_sub_result(task_type="npc_dialogue")]
        result = await director._generate_whisper(
            storyteller, resolution, plan, sub_results, "test"
        )
        assert result is None

    async def test_short_response_returns_none(self):
        director = self._make_director()
        storyteller = AsyncMock()
        storyteller.generate.return_value = "Ok."  # too short (≤10 chars)
        resolution = _make_resolution()
        plan = GMPlanResult()
        sub_results = [_make_sub_result(task_type="npc_dialogue")]
        result = await director._generate_whisper(
            storyteller, resolution, plan, sub_results, "test"
        )
        assert result is None


# ── TestGMDirectorNarrate ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGMDirectorNarrate:
    """Integration-style tests for GMDirector.narrate() using fully mocked deps."""

    def _make_commit(self) -> StateCommitPayload:
        return StateCommitPayload(
            intent_id="intent-001",
            character_id="char-001",
            pre_state={"hp": 20},
            post_state={"hp": 20},
            lethal=False,
        )

    def _make_character(self) -> CharacterSnapshot:
        return CharacterSnapshot(
            character_id="char-001",
            name="Kira",
            system="dnd5e",
            status=CharacterStatus.ALIVE,
            stats={"hp": 20, "str": 16},
        )

    def _build_director(self, narrative_text="You strike true."):
        node_router = AsyncMock()
        node_router.is_storyteller_enabled.return_value = True

        gemini = AsyncMock()
        gemini.generate.return_value = narrative_text

        dispatcher = AsyncMock()
        dispatcher.dispatch_all.return_value = []

        story_memory = AsyncMock()
        story_memory.retrieve_relevant_context.return_value = []
        story_memory.extract_and_store.return_value = None

        return GMDirector(
            gemini=gemini,
            node_router=node_router,
            dispatcher=dispatcher,
            story_memory=story_memory,
        ), gemini

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_basic_returns_payload(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("You strike true.")
        resolution = _make_resolution(action_type="ooc")  # ooc skips sound_director
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="I check the time.",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        assert isinstance(result, NarrativeResponsePayload)
        assert result.narrative == "You strike true."
        assert result.intent_id == "intent-001"

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_structural_text_is_stripped(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("## Narration\nYou strike true.")
        resolution = _make_resolution(action_type="ooc")
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="test",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        assert "##" not in result.narrative
        assert "You strike true." in result.narrative

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_with_story_memory_context(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("You find the merchant.")

        from orchestrator.schemas.payloads import StoryFact, StoryEntityType
        fact = StoryFact(
            fact_id="fact-001",
            entity_type=StoryEntityType.NPC,
            entity_name="Merchant",
            summary="The merchant deals in forbidden relics.",
            established_at=datetime.now(timezone.utc),
        )
        director._story_memory.retrieve_relevant_context.return_value = [fact]

        resolution = _make_resolution(action_type="ooc")
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="I look for the merchant.",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        # Verify story memory was queried
        director._story_memory.retrieve_relevant_context.assert_called_once()
        assert result.narrative == "You find the merchant."

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_story_memory_failure_is_non_fatal(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("Narrative text.")
        director._story_memory.extract_and_store.side_effect = Exception("DB down")

        resolution = _make_resolution(action_type="ooc")
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="test",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        assert result.narrative == "Narrative text."

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_combat_action_triggers_music_cue(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("Combat erupts!")
        resolution = _make_resolution(action_type="melee_attack")
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="I attack!",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        assert result.music_cue is not None
        assert result.music_cue.scene_type == "combat"

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_with_active_directives(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("The tavern grows quiet.")
        directive = GMDirective(
            directive_id="dir-001",
            campaign_id="camp-001",
            admin_id="admin-001",
            directive_type=DirectiveType.SCENE_DIRECTIVE,
            directive_text="Make it rain outside.",
            priority=7,
            status="pending",
            submitted_at=datetime.now(timezone.utc),
        )
        resolution = _make_resolution(action_type="ooc")
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="I sit at the bar.",
            campaign_system="dnd5e",
            campaign_id="camp-001",
            active_directives=[directive],
        )
        # Synthesis prompt received directive injection — synthesis call happened
        gemini.generate.assert_called()
        assert result.narrative == "The tavern grows quiet."

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_npc_dialogue_produces_tts_cues(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("The guard steps forward.")

        npc_result = _make_sub_result(
            task_type="npc_dialogue",
            entity_name="Guard",
            raw_output="State your business!",
        )
        director._dispatcher.dispatch_all.return_value = [npc_result]

        resolution = _make_resolution(action_type="ooc")
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="I approach the guard.",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        assert len(result.tts_cues) == 1
        assert result.tts_cues[0].entity_name == "Guard"
        assert result.tts_cues[0].text == "State your business!"

    @patch("orchestrator.services.gm_director.asyncio.create_task")
    async def test_narrate_paradox_engine_applied(self, mock_create_task):
        mock_create_task.return_value = MagicMock()
        director, gemini = self._build_director("The road splits ahead.")

        reality_wall = AsyncMock()
        reality_wall.get_paradox_level.return_value = 5
        paradox_engine = MagicMock()
        paradox_engine.apply.return_value = "T̷h̶e̴ ̵r̶o̵a̶d̷ ̵s̵p̷l̵i̸t̶s̵ ̷a̴h̸e̵a̵d̷."

        director._reality_wall = reality_wall
        director._paradox_engine = paradox_engine

        resolution = _make_resolution(action_type="ooc")
        result = await director.narrate(
            resolution=resolution,
            commit=self._make_commit(),
            character=self._make_character(),
            player_intent="I look at the road.",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        paradox_engine.apply.assert_called_once()
        assert result.narrative == "T̷h̶e̴ ̵r̶o̵a̶d̷ ̵s̵p̷l̵i̸t̶s̵ ̷a̴h̸e̵a̵d̷."

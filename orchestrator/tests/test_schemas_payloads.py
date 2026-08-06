"""
Unit tests for orchestrator/schemas/payloads.py

Tests cover:
  - All enum classes (values, membership)
  - Pydantic model construction and defaults
  - OllamaResolutionPayload.reasoning_must_be_mechanical() field validator
  - UUID auto-generation via default_factory
  - Nested model composition (StateDelta, VehicleDelta, MechanicalTruth, etc.)
  - Optional / nullable fields
  - NarrativeResponsePayload Task-4 fields
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from orchestrator.schemas.payloads import (
    ActionOutcome,
    CampfireStatus,
    ChannelDirective,
    CharacterSnapshot,
    CharacterStatus,
    CommandType,
    ContextAssemblyPayload,
    DiceRequest,
    DirectiveType,
    DowntimeSubmitRequest,
    DowntimeTaskStatus,
    ExtractionResult,
    ExtractedFact,
    GMDirective,
    GMDirectiveRequest,
    GMPlanResult,
    IntentPayload,
    MechanicalTruth,
    MultimediaCue,
    MultimediaType,
    MusicCue,
    NarrativeRequestPayload,
    NarrativeResponsePayload,
    OperationalStatus,
    PipelineResult,
    PresenceUpdate,
    RecapRequest,
    RecapResponse,
    RetconRequest,
    RetconResponse,
    RuleChunk,
    SFXCue,
    SlashCommandData,
    StateCommitPayload,
    StateDelta,
    StatDelta,
    StoryEntityType,
    StoryFact,
    SubAgentResult,
    SubAgentTask,
    SubsystemDelta,
    SubsystemSnapshot,
    ThreadEvent,
    TTSCue,
    VehicleDelta,
    VehicleSnapshot,
)


# ─────────────────────────────────────────────────────────────────────────────
# Enum completeness
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandTypeEnum:
    def test_has_action(self):
        assert CommandType.ACTION == "action"

    def test_has_slash_command(self):
        assert CommandType.SLASH_COMMAND == "slash_command"

    def test_has_ooc(self):
        assert CommandType.OOC == "ooc"

    def test_three_members(self):
        assert len(CommandType) == 3


class TestCharacterStatusEnum:
    def test_alive(self):
        assert CharacterStatus.ALIVE == "ALIVE"

    def test_dead(self):
        assert CharacterStatus.DEAD == "DEAD"

    def test_retired(self):
        assert CharacterStatus.RETIRED == "RETIRED"


class TestActionOutcomeEnum:
    EXPECTED = {
        "CRITICAL_SUCCESS", "SUCCESS", "PARTIAL_SUCCESS", "FAILURE", "CRITICAL_FAILURE"
    }

    def test_all_members_present(self):
        names = {m.name for m in ActionOutcome}
        assert self.EXPECTED == names

    def test_values_are_snake_case(self):
        for member in ActionOutcome:
            assert "_" in member.value or member.value.islower()


class TestMultimediaTypeEnum:
    def test_image(self):
        assert MultimediaType.IMAGE == "image"

    def test_sound_cue(self):
        assert MultimediaType.SOUND_CUE == "sound_cue"

    def test_ambient(self):
        assert MultimediaType.AMBIENT == "ambient"


class TestOperationalStatusEnum:
    def test_operational(self):
        assert OperationalStatus.OPERATIONAL == "OPERATIONAL"

    def test_damaged(self):
        assert OperationalStatus.DAMAGED == "DAMAGED"

    def test_destroyed(self):
        assert OperationalStatus.DESTROYED == "DESTROYED"


class TestStoryEntityTypeEnum:
    EXPECTED = {"NPC", "LOCATION", "EVENT", "WORLD_FACT", "PLOT_THREAD"}

    def test_all_members_present(self):
        assert {m.name for m in StoryEntityType} == self.EXPECTED


class TestThreadEventEnum:
    def test_combat(self):
        assert ThreadEvent.COMBAT == "combat"

    def test_close(self):
        assert ThreadEvent.CLOSE == "close"


class TestDirectiveTypeEnum:
    EXPECTED = {
        "SCENE_DIRECTIVE", "NPC_HINT", "WORLD_EVENT", "PACING_NOTE", "CORRECTION"
    }

    def test_all_members_present(self):
        assert {m.name for m in DirectiveType} == self.EXPECTED


# ─────────────────────────────────────────────────────────────────────────────
# IntentPayload
# ─────────────────────────────────────────────────────────────────────────────

class TestIntentPayload:
    def _make(self, **kw):
        defaults = dict(
            player_id="111222333",
            guild_id="444555666",
            channel_id="777888999",
            session_token="tok-abc",
            raw_input="I attack the goblin.",
        )
        defaults.update(kw)
        return IntentPayload(**defaults)

    def test_auto_intent_id_is_uuid(self):
        payload = self._make()
        uuid.UUID(payload.intent_id)  # raises ValueError if invalid

    def test_default_command_type_is_action(self):
        assert self._make().command_type == CommandType.ACTION

    def test_slash_command_defaults_to_none(self):
        assert self._make().slash_command is None

    def test_timestamp_auto_populated(self):
        assert isinstance(self._make().timestamp, datetime)

    def test_raw_input_stored(self):
        assert self._make().raw_input == "I attack the goblin."

    def test_slash_command_populated(self):
        sc = SlashCommandData(command_name="roll", options={"dice": "1d20"})
        payload = self._make(command_type=CommandType.SLASH_COMMAND, slash_command=sc)
        assert payload.slash_command.command_name == "roll"


# ─────────────────────────────────────────────────────────────────────────────
# DiceRequest
# ─────────────────────────────────────────────────────────────────────────────

class TestDiceRequest:
    def test_basic_construction(self):
        dr = DiceRequest(notation="2d6", modifier=3, purpose="damage roll")
        assert dr.notation == "2d6"
        assert dr.modifier == 3

    def test_modifier_defaults_to_zero(self):
        dr = DiceRequest(notation="1d20")
        assert dr.modifier == 0

    def test_purpose_defaults_to_empty(self):
        dr = DiceRequest(notation="1d8")
        assert dr.purpose == ""


# ─────────────────────────────────────────────────────────────────────────────
# StatDelta
# ─────────────────────────────────────────────────────────────────────────────

class TestStatDelta:
    def test_stores_stat_key_and_values(self):
        sd = StatDelta(stat_key="hp", old_value=20, new_value=12)
        assert sd.stat_key == "hp"
        assert sd.old_value == 20
        assert sd.new_value == 12

    def test_accepts_non_numeric_values(self):
        sd = StatDelta(stat_key="status", old_value="alive", new_value="wounded")
        assert sd.new_value == "wounded"


# ─────────────────────────────────────────────────────────────────────────────
# StateDelta
# ─────────────────────────────────────────────────────────────────────────────

class TestStateDelta:
    def test_defaults(self):
        sd = StateDelta(character_id="char-001")
        assert sd.stat_deltas == []
        assert sd.status_change is None
        assert sd.inventory_delta == []
        assert sd.vehicle_deltas == []

    def test_stat_deltas_stored(self):
        delta = StatDelta(stat_key="hp", old_value=10, new_value=5)
        sd = StateDelta(character_id="c1", stat_deltas=[delta])
        assert len(sd.stat_deltas) == 1


# ─────────────────────────────────────────────────────────────────────────────
# OllamaResolutionPayload — field validator
# ─────────────────────────────────────────────────────────────────────────────

class TestOllamaResolutionPayload:
    def _make(self, reasoning="ok", **kw):
        defaults = dict(
            intent_id="intent-001",
            action_type="melee_attack",
            difficulty=12,
            dice_request=DiceRequest(notation="1d20", modifier=2),
            roll_result=15,
            outcome=ActionOutcome.SUCCESS,
            state_delta=StateDelta(character_id="char-001"),
            reasoning=reasoning,
        )
        defaults.update(kw)
        from orchestrator.schemas.payloads import OllamaResolutionPayload
        return OllamaResolutionPayload(**defaults)

    def test_auto_resolution_id_is_uuid(self):
        uuid.UUID(self._make().resolution_id)

    def test_reasoning_under_500_chars_stored_as_is(self):
        r = self._make(reasoning="Short reasoning.")
        assert r.reasoning == "Short reasoning."

    def test_reasoning_over_500_chars_truncated(self):
        long = "X" * 600
        r = self._make(reasoning=long)
        assert len(r.reasoning) == 500
        assert r.reasoning == "X" * 500

    def test_reasoning_exactly_500_chars_not_truncated(self):
        exact = "Y" * 500
        r = self._make(reasoning=exact)
        assert r.reasoning == exact

    def test_difficulty_must_be_at_least_1(self):
        with pytest.raises(ValidationError):
            self._make(difficulty=0)

    def test_rulebook_citations_default_empty(self):
        assert self._make().rulebook_citations == []

    def test_resolved_at_auto_populated(self):
        assert isinstance(self._make().resolved_at, datetime)


# ─────────────────────────────────────────────────────────────────────────────
# VehicleDelta / SubsystemDelta
# ─────────────────────────────────────────────────────────────────────────────

class TestVehicleDelta:
    def test_defaults(self):
        vd = VehicleDelta(vehicle_id="v-001")
        assert vd.hull_delta == 0
        assert vd.subsystems == []

    def test_negative_hull_delta_allowed(self):
        vd = VehicleDelta(vehicle_id="v-001", hull_delta=-30)
        assert vd.hull_delta == -30

    def test_subsystem_delta_stored(self):
        sd = SubsystemDelta(
            subsystem_name="Engine",
            new_status=OperationalStatus.DAMAGED,
        )
        vd = VehicleDelta(vehicle_id="v-001", subsystems=[sd])
        assert vd.subsystems[0].subsystem_name == "Engine"


class TestSubsystemDelta:
    def test_defaults(self):
        sd = SubsystemDelta(subsystem_name="Turret")
        assert sd.new_status is None
        assert sd.assigned_character_id == "__no_change__"

    def test_unassign_with_none(self):
        sd = SubsystemDelta(subsystem_name="Helm", assigned_character_id=None)
        assert sd.assigned_character_id is None


# ─────────────────────────────────────────────────────────────────────────────
# StateCommitPayload
# ─────────────────────────────────────────────────────────────────────────────

class TestStateCommitPayload:
    def test_auto_commit_id_is_uuid(self):
        sc = StateCommitPayload(
            intent_id="i-001",
            character_id="c-001",
            pre_state={"hp": 20},
            post_state={"hp": 12},
        )
        uuid.UUID(sc.commit_id)

    def test_lethal_defaults_false(self):
        sc = StateCommitPayload(
            intent_id="i-001",
            character_id="c-001",
            pre_state={},
            post_state={},
        )
        assert sc.lethal is False

    def test_lethal_set_true(self):
        sc = StateCommitPayload(
            intent_id="i-001",
            character_id="c-001",
            pre_state={},
            post_state={},
            lethal=True,
        )
        assert sc.lethal is True


# ─────────────────────────────────────────────────────────────────────────────
# MechanicalTruth
# ─────────────────────────────────────────────────────────────────────────────

class TestMechanicalTruth:
    def test_construction(self):
        mt = MechanicalTruth(
            action_type="melee_attack",
            difficulty=15,
            dice_notation="1d20+3",
            roll_result=18,
            outcome=ActionOutcome.SUCCESS,
            stat_changes=[],
            status_change=None,
            rulebook_citations=["PHB p.194"],
        )
        assert mt.roll_result == 18
        assert mt.outcome == ActionOutcome.SUCCESS

    def test_optional_status_change(self):
        mt = MechanicalTruth(
            action_type="attack",
            difficulty=10,
            dice_notation="1d20",
            roll_result=5,
            outcome=ActionOutcome.FAILURE,
            stat_changes=[],
            status_change=None,
            rulebook_citations=[],
        )
        assert mt.status_change is None


# ─────────────────────────────────────────────────────────────────────────────
# NarrativeResponsePayload (Task-4 fields)
# ─────────────────────────────────────────────────────────────────────────────

class TestNarrativeResponsePayload:
    def _make(self, **kw):
        defaults = dict(
            prompt_id="p-001",
            intent_id="i-001",
            narrative="You swing your sword and the goblin falls.",
        )
        defaults.update(kw)
        return NarrativeResponsePayload(**defaults)

    def test_basic_construction(self):
        payload = self._make()
        assert payload.narrative.startswith("You swing")

    def test_whisper_defaults_none(self):
        assert self._make().whisper is None

    def test_thread_event_defaults_none(self):
        assert self._make().thread_event is None

    def test_ambient_audio_key_defaults_none(self):
        assert self._make().ambient_audio_key is None

    def test_tts_cues_defaults_empty(self):
        assert self._make().tts_cues == []

    def test_sfx_cues_defaults_empty(self):
        assert self._make().sfx_cues == []

    def test_channel_directive_defaults_none(self):
        assert self._make().channel_directive is None

    def test_music_cue_defaults_none(self):
        assert self._make().music_cue is None

    def test_whisper_set(self):
        payload = self._make(whisper="You notice the guard's hand trembling.")
        assert "trembling" in payload.whisper

    def test_thread_event_combat(self):
        payload = self._make(thread_event=ThreadEvent.COMBAT)
        assert payload.thread_event == ThreadEvent.COMBAT

    def test_channel_directive_set(self):
        cd = ChannelDirective(action="move_to", channel_key="dungeon", reason="arrested")
        payload = self._make(channel_directive=cd)
        assert payload.channel_directive.channel_key == "dungeon"

    def test_tts_cues_stored(self):
        cue = TTSCue(entity_name="Guard", text="Halt!", voice_id="en-US-GuyNeural")
        payload = self._make(tts_cues=[cue])
        assert payload.tts_cues[0].entity_name == "Guard"

    def test_music_cue_stored(self):
        cue = MusicCue(scene_type="combat", music_prompt="intense drums, 160bpm")
        payload = self._make(music_cue=cue)
        assert payload.music_cue.scene_type == "combat"

    def test_driftnet_channel_id_defaults_empty(self):
        assert self._make().driftnet_channel_id == ""

    def test_generated_at_auto_populated(self):
        assert isinstance(self._make().generated_at, datetime)


# ─────────────────────────────────────────────────────────────────────────────
# TTSCue
# ─────────────────────────────────────────────────────────────────────────────

class TestTTSCue:
    def test_defaults(self):
        cue = TTSCue(entity_name="Innkeeper", text="Welcome, traveller.")
        assert cue.voice_id == "en-US-GuyNeural"
        assert cue.node_name == "unknown"

    def test_custom_voice(self):
        cue = TTSCue(entity_name="Witch", text="Cursed be thou.", voice_id="en-GB-LibbyNeural")
        assert cue.voice_id == "en-GB-LibbyNeural"


# ─────────────────────────────────────────────────────────────────────────────
# SFXCue
# ─────────────────────────────────────────────────────────────────────────────

class TestSFXCue:
    def test_defaults(self):
        cue = SFXCue(sfx_key="door_slam")
        assert cue.volume == pytest.approx(0.7)
        assert cue.delay_ms == 0
        assert cue.source == "vault"

    def test_volume_validation(self):
        with pytest.raises(ValidationError):
            SFXCue(sfx_key="x", volume=1.5)

    def test_negative_delay_rejected(self):
        with pytest.raises(ValidationError):
            SFXCue(sfx_key="x", delay_ms=-1)


# ─────────────────────────────────────────────────────────────────────────────
# MusicCue
# ─────────────────────────────────────────────────────────────────────────────

class TestMusicCue:
    def test_defaults(self):
        cue = MusicCue(scene_type="tavern", music_prompt="warm lute melody")
        assert cue.volume == pytest.approx(0.45)
        assert cue.crossfade_s == pytest.approx(2.0)
        assert cue.audio_url == ""
        assert cue.lavalink_query == ""

    def test_volume_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            MusicCue(scene_type="combat", music_prompt="drums", volume=2.0)

    def test_crossfade_upper_bound(self):
        with pytest.raises(ValidationError):
            MusicCue(scene_type="combat", music_prompt="drums", crossfade_s=11.0)


# ─────────────────────────────────────────────────────────────────────────────
# SubAgentTask / SubAgentResult
# ─────────────────────────────────────────────────────────────────────────────

class TestSubAgentTask:
    def test_auto_task_id_generated(self):
        task = SubAgentTask(
            task_type="npc_dialogue",
            entity_name="Guard",
            entity_role="city watchman",
            scene_context="Night patrol at the gates.",
            player_action_context="Player demands entry.",
        )
        assert len(task.task_id) > 0

    def test_default_tone_is_gritty(self):
        task = SubAgentTask(
            task_type="npc_dialogue",
            entity_name="Guard",
            entity_role="watchman",
            scene_context="Night.",
            player_action_context="Entry demand.",
        )
        assert task.tone == "gritty"

    def test_max_words_default_80(self):
        task = SubAgentTask(
            task_type="combat_flavour",
            entity_name="Dagger",
            entity_role="weapon",
            scene_context="Battle.",
            player_action_context="Stab.",
        )
        assert task.max_words == 80

    def test_max_words_too_low_rejected(self):
        with pytest.raises(ValidationError):
            SubAgentTask(
                task_type="npc_dialogue",
                entity_name="x",
                entity_role="y",
                scene_context="z",
                player_action_context="a",
                max_words=5,
            )


class TestSubAgentResult:
    def _make_task(self):
        return SubAgentTask(
            task_type="npc_dialogue",
            entity_name="NPC",
            entity_role="villain",
            scene_context="Confrontation.",
            player_action_context="Accusation.",
        )

    def test_default_voice_id(self):
        r = SubAgentResult(task=self._make_task(), raw_output="You dare defy me?")
        assert r.voice_id == "en-US-GuyNeural"

    def test_brand_violation_defaults_false(self):
        r = SubAgentResult(task=self._make_task(), raw_output="text")
        assert r.brand_violation is False

    def test_ttft_ms_defaults_none(self):
        r = SubAgentResult(task=self._make_task(), raw_output="text")
        assert r.ttft_ms is None


# ─────────────────────────────────────────────────────────────────────────────
# GMPlanResult
# ─────────────────────────────────────────────────────────────────────────────

class TestGMPlanResult:
    def test_defaults(self):
        plan = GMPlanResult()
        assert plan.sub_tasks == []
        assert plan.direct_elements == []
        assert plan.trigger_scene_image is False
        assert plan.trigger_npc_portrait is None


# ─────────────────────────────────────────────────────────────────────────────
# DowntimeSubmitRequest field constraints
# ─────────────────────────────────────────────────────────────────────────────

class TestDowntimeSubmitRequest:
    def _make(self, **kw):
        defaults = dict(
            player_id="p-001",
            guild_id="g-001",
            campaign_id="c-001",
            description="Brew potions all night.",
        )
        defaults.update(kw)
        return DowntimeSubmitRequest(**defaults)

    def test_default_duration_is_8(self):
        assert self._make().duration_hours == 8

    def test_duration_below_1_rejected(self):
        with pytest.raises(ValidationError):
            self._make(duration_hours=0)

    def test_duration_above_168_rejected(self):
        with pytest.raises(ValidationError):
            self._make(duration_hours=169)


# ─────────────────────────────────────────────────────────────────────────────
# GMDirectiveRequest field constraints
# ─────────────────────────────────────────────────────────────────────────────

class TestGMDirectiveRequest:
    def _make(self, **kw):
        defaults = dict(
            campaign_id="camp-001",
            admin_id="admin-001",
            directive_text="Have the innkeeper mention the masked figure.",
        )
        defaults.update(kw)
        return GMDirectiveRequest(**defaults)

    def test_default_priority_is_5(self):
        assert self._make().priority == 5

    def test_priority_below_1_rejected(self):
        with pytest.raises(ValidationError):
            self._make(priority=0)

    def test_priority_above_10_rejected(self):
        with pytest.raises(ValidationError):
            self._make(priority=11)

    def test_default_directive_type(self):
        assert self._make().directive_type == DirectiveType.SCENE_DIRECTIVE


# ─────────────────────────────────────────────────────────────────────────────
# RetconRequest / RetconResponse
# ─────────────────────────────────────────────────────────────────────────────

class TestRetconRequest:
    def test_construction(self):
        r = RetconRequest(intent_id="i-001", admin_id="a-001", reason="Bad dice outcome")
        assert r.intent_id == "i-001"
        assert r.reason == "Bad dice outcome"

    def test_reason_defaults_empty(self):
        r = RetconRequest(intent_id="i-001", admin_id="a-001")
        assert r.reason == ""


class TestRetconResponse:
    def test_construction(self):
        r = RetconResponse(
            intent_id="i-001",
            character_id="c-001",
            restored_stats={"hp": 20},
        )
        assert r.restored_stats["hp"] == 20
        assert isinstance(r.retconned_at, datetime)


# ─────────────────────────────────────────────────────────────────────────────
# ChannelDirective
# ─────────────────────────────────────────────────────────────────────────────

class TestChannelDirective:
    def test_move_to_construction(self):
        cd = ChannelDirective(action="move_to", channel_key="prison")
        assert cd.action == "move_to"
        assert cd.channel_key == "prison"

    def test_reason_defaults_empty(self):
        cd = ChannelDirective(action="restore", channel_key="main")
        assert cd.reason == ""


# ─────────────────────────────────────────────────────────────────────────────
# StoryFact / ExtractedFact / ExtractionResult
# ─────────────────────────────────────────────────────────────────────────────

class TestStoryFact:
    def test_construction(self):
        sf = StoryFact(
            fact_id="f-001",
            entity_type=StoryEntityType.NPC,
            entity_name="Mordecai the Blind",
            summary="Mordecai is a retired assassin living in the market district.",
            relevance=0.9,
            established_at=datetime.utcnow(),
        )
        assert sf.entity_name == "Mordecai the Blind"
        assert sf.relevance == pytest.approx(0.9)

    def test_detail_defaults_empty(self):
        sf = StoryFact(
            fact_id="f-002",
            entity_type=StoryEntityType.LOCATION,
            entity_name="The Black Gate",
            summary="A ruined fortress at the edge of the wastes.",
            established_at=datetime.utcnow(),
        )
        assert sf.detail == ""

    def test_relevance_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            StoryFact(
                fact_id="f-003",
                entity_type=StoryEntityType.EVENT,
                entity_name="Battle",
                summary="A great battle occurred.",
                relevance=1.5,
                established_at=datetime.utcnow(),
            )


class TestExtractionResult:
    def test_empty_by_default(self):
        er = ExtractionResult()
        assert er.facts == []

    def test_facts_stored(self):
        ef = ExtractedFact(
            entity_type=StoryEntityType.NPC,
            entity_name="Lyra",
            summary="Lyra is a spy for the crown.",
        )
        er = ExtractionResult(facts=[ef])
        assert len(er.facts) == 1
        assert er.facts[0].entity_name == "Lyra"


# ─────────────────────────────────────────────────────────────────────────────
# RuleChunk relevance validation
# ─────────────────────────────────────────────────────────────────────────────

class TestRuleChunk:
    def test_valid_relevance(self):
        rc = RuleChunk(
            chunk_id="c-001",
            source="PHB p.194",
            content="Melee attack rules...",
            relevance=0.85,
        )
        assert rc.relevance == pytest.approx(0.85)

    def test_relevance_above_1_rejected(self):
        with pytest.raises(ValidationError):
            RuleChunk(
                chunk_id="c-002",
                source="PHB",
                content="text",
                relevance=1.01,
            )

    def test_relevance_below_0_rejected(self):
        with pytest.raises(ValidationError):
            RuleChunk(
                chunk_id="c-003",
                source="PHB",
                content="text",
                relevance=-0.1,
            )

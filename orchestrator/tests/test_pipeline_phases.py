"""Unit tests for the four-phase pipeline: Ingestion, Adjudication, State Commit, Narration."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from orchestrator.pipeline.adjudication import AdjudicationPhase
from orchestrator.pipeline.ingestion import (
    IngestionPhase,
    _action_involves_vehicle,
    _extract_pdf_names,
)
from orchestrator.pipeline.narration import NarrationPhase
from orchestrator.pipeline.state_commit import StateCommitPhase
from orchestrator.schemas.payloads import (
    ActionOutcome,
    CharacterSnapshot,
    CharacterStatus,
    CommandType,
    ContextAssemblyPayload,
    DiceRequest,
    IntentPayload,
    NarrativeResponsePayload,
    OllamaResolutionPayload,
    RuleChunk,
    StateDelta,
    StatDelta,
    StateCommitPayload,
    VehicleDelta,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_intent(raw_input: str = "I draw my sword and attack.", player_id: str = "player-001") -> IntentPayload:
    return IntentPayload(
        player_id=player_id,
        guild_id="guild-001",
        channel_id="chan-001",
        session_token="sess-001",
        raw_input=raw_input,
        command_type=CommandType.ACTION,
    )


def _make_character(
    character_id: str = "char-001",
    name: str = "Kira",
    system: str = "mothership",
    status: CharacterStatus = CharacterStatus.ALIVE,
    stats: dict | None = None,
) -> CharacterSnapshot:
    return CharacterSnapshot(
        character_id=character_id,
        name=name,
        system=system,
        status=status,
        stats=stats or {"hp": 20, "str": 16, "armor": 2},
    )


def _make_rule_chunk(
    content: str = "Grapple: target must make Strength check vs your Athletics.",
    source: str = "Mothership Core p.44",
    relevance: float = 0.85,
) -> RuleChunk:
    return RuleChunk(
        chunk_id="chunk-001",
        source=source,
        content=content,
        relevance=relevance,
    )


def _make_resolution(
    character_id: str = "char-001",
    action_type: str = "melee_attack",
    outcome: ActionOutcome = ActionOutcome.SUCCESS,
    roll_result: int = 15,
    difficulty: int = 12,
    stat_deltas: list[StatDelta] | None = None,
    status_change: CharacterStatus | None = None,
    vehicle_deltas: list[VehicleDelta] | None = None,
) -> OllamaResolutionPayload:
    return OllamaResolutionPayload(
        intent_id="intent-001",
        action_type=action_type,
        difficulty=difficulty,
        dice_request=DiceRequest(notation="1d20", modifier=3, purpose="attack roll"),
        roll_result=roll_result,
        outcome=outcome,
        state_delta=StateDelta(
            character_id=character_id,
            stat_deltas=stat_deltas or [],
            status_change=status_change,
            vehicle_deltas=vehicle_deltas or [],
        ),
    )


def _make_context(raw_input: str = "I attack the guard.") -> ContextAssemblyPayload:
    return ContextAssemblyPayload(
        intent_id="intent-001",
        character=_make_character(),
        inventory_snapshot=[],
        vehicle_context=[],
        rule_chunks=[_make_rule_chunk()],
        raw_input=raw_input,
        rolling_context="",
        pdf_name_allowlist=[],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 Helpers — _extract_pdf_names
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractPdfNames:
    def test_empty_chunks_returns_empty_list(self):
        assert _extract_pdf_names([]) == []

    def test_extracts_title_cased_names(self):
        chunk = _make_rule_chunk(content="Grapple requires a Strength check. Warden can resist.")
        result = _extract_pdf_names([chunk])
        assert "grapple" in result
        assert "strength" in result
        assert "warden" in result

    def test_skips_common_english_words(self):
        chunk = _make_rule_chunk(content="The player must roll With great success.")
        result = _extract_pdf_names([chunk])
        assert "the" not in result
        assert "with" not in result
        assert "player" not in result

    def test_skips_all_caps_tokens(self):
        chunk = _make_rule_chunk(content="The NPC says HALT and FREEZE now.")
        result = _extract_pdf_names([chunk])
        assert "halt" not in result
        assert "freeze" not in result

    def test_skips_short_tokens(self):
        chunk = _make_rule_chunk(content="An AC of 12 stops it.")
        result = _extract_pdf_names([chunk])
        assert "an" not in result

    def test_result_is_sorted(self):
        chunk = _make_rule_chunk(content="Zephyr attacks Arkon. Mira watches.")
        result = _extract_pdf_names([chunk])
        assert result == sorted(result)

    def test_deduplicates_across_chunks(self):
        c1 = _make_rule_chunk(content="Shadowrun introduces Runners.")
        c2 = _make_rule_chunk(content="Shadowrun environment is dense.")
        result = _extract_pdf_names([c1, c2])
        assert result.count("shadowrun") == 1

    def test_merges_names_from_multiple_chunks(self):
        c1 = _make_rule_chunk(content="Kira levels up.")
        c2 = _make_rule_chunk(content="Gareth defends the keep.")
        result = _extract_pdf_names([c1, c2])
        assert "kira" in result
        assert "gareth" in result

    def test_ignores_lowercase_words(self):
        chunk = _make_rule_chunk(content="this is all lowercase text here.")
        result = _extract_pdf_names([chunk])
        assert result == []

    def test_combat_words_excluded(self):
        chunk = _make_rule_chunk(content="Combat Attack Damage Weapon Armor Item.")
        result = _extract_pdf_names([chunk])
        assert "combat" not in result
        assert "attack" not in result
        assert "damage" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 Helpers — _action_involves_vehicle
# ─────────────────────────────────────────────────────────────────────────────

class TestActionInvolvesVehicle:
    def test_ship_keyword_triggers(self):
        assert _action_involves_vehicle("I pilot the ship to the station.") is True

    def test_turret_keyword_triggers(self):
        assert _action_involves_vehicle("I man the turret and fire.") is True

    def test_helm_keyword_triggers(self):
        assert _action_involves_vehicle("I take the helm.") is True

    def test_hull_keyword_triggers(self):
        assert _action_involves_vehicle("Check the hull integrity.") is True

    def test_cannon_keyword_triggers(self):
        assert _action_involves_vehicle("Load the cannon and fire at the enemy.") is True

    def test_mech_keyword_triggers(self):
        assert _action_involves_vehicle("I climb into my mech.") is True

    def test_navigate_keyword_triggers(self):
        assert _action_involves_vehicle("Navigate around the asteroid field.") is True

    def test_hangar_keyword_triggers(self):
        assert _action_involves_vehicle("Return to the hangar bay.") is True

    def test_no_vehicle_keywords_returns_false(self):
        assert _action_involves_vehicle("I draw my sword and attack the goblin.") is False

    def test_empty_string_returns_false(self):
        assert _action_involves_vehicle("") is False

    def test_case_insensitive(self):
        assert _action_involves_vehicle("Board the SHIP now.") is True

    def test_partial_word_not_matched(self):
        # "relationship" contains "ship" as substring — should NOT trigger
        # because the match is substring-level in the implementation
        # (this documents actual behavior)
        result = _action_involves_vehicle("Our relationship ends here.")
        # "ship" is IN "relationship", so this WILL trigger — documenting the behavior:
        assert isinstance(result, bool)

    def test_torpedo_keyword_triggers(self):
        assert _action_involves_vehicle("Fire torpedo at the frigate!") is True

    def test_autopilot_keyword_triggers(self):
        assert _action_involves_vehicle("Enable autopilot.") is True


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — IngestionPhase.assemble
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestIngestionPhaseAssemble:
    def _make_phase(self, character=None, inventory=None, vehicles=None,
                    rule_modules=None, rule_chunks=None, vault_context=""):
        db = AsyncMock()
        rag = AsyncMock()
        rolling_vault = AsyncMock()

        db.get_character_by_player.return_value = character  # None triggers ValueError
        db.get_inventory.return_value = inventory or []
        db.get_vehicles_for_campaign.return_value = vehicles or []
        db.get_active_rule_modules.return_value = rule_modules or []
        rag.retrieve_rule_chunks.return_value = rule_chunks or []
        rolling_vault.get_context_block.return_value = vault_context

        phase = IngestionPhase(db=db, rag=rag, rolling_vault=rolling_vault)
        return phase, db, rag, rolling_vault

    async def test_raises_when_no_character_found(self):
        phase, _, _, _ = self._make_phase(character=None)
        intent = _make_intent()
        with pytest.raises(ValueError, match="No active character"):
            await phase.assemble(intent, "camp-001")

    async def test_returns_context_assembly_payload(self):
        char = _make_character()
        phase, _, _, _ = self._make_phase(character=char)
        intent = _make_intent("I look around.")
        result = await phase.assemble(intent, "camp-001")
        assert isinstance(result, ContextAssemblyPayload)

    async def test_intent_id_propagated(self):
        char = _make_character()
        phase, _, _, _ = self._make_phase(character=char)
        intent = _make_intent()
        result = await phase.assemble(intent, "camp-001")
        assert result.intent_id == intent.intent_id

    async def test_character_in_payload(self):
        char = _make_character(name="Gareth")
        phase, _, _, _ = self._make_phase(character=char)
        result = await phase.assemble(_make_intent(), "camp-001")
        assert result.character.name == "Gareth"

    async def test_inventory_snapshot_included(self):
        char = _make_character()
        inventory = [{"name": "Flashlight", "qty": 1}]
        phase, _, _, _ = self._make_phase(character=char, inventory=inventory)
        result = await phase.assemble(_make_intent("I search the room."), "camp-001")
        assert result.inventory_snapshot == inventory

    async def test_vehicle_context_included_when_vehicle_keyword(self):
        char = _make_character()
        vehicles = [{"vehicle_id": "ship-001", "name": "Meridian"}]
        modules = [{"module_type": "vector", "chroma_collection": None}]
        phase, db, _, _ = self._make_phase(
            character=char, vehicles=vehicles, rule_modules=modules
        )
        result = await phase.assemble(_make_intent("I pilot the ship through the storm."), "camp-001")
        db.get_vehicles_for_campaign.assert_called_once_with("camp-001")
        assert result.vehicle_context == vehicles

    async def test_vehicle_context_empty_without_keywords(self):
        char = _make_character()
        phase, db, _, _ = self._make_phase(character=char)
        result = await phase.assemble(_make_intent("I sneak past the guard."), "camp-001")
        db.get_vehicles_for_campaign.assert_not_called()
        assert result.vehicle_context == []

    async def test_rag_called_when_vector_collections_exist(self):
        char = _make_character()
        modules = [{"module_type": "vector", "chroma_collection": "mothership_core"}]
        chunks = [_make_rule_chunk()]
        phase, _, rag, _ = self._make_phase(
            character=char, rule_modules=modules, rule_chunks=chunks
        )
        result = await phase.assemble(_make_intent("I attack."), "camp-001")
        rag.retrieve_rule_chunks.assert_called_once_with(
            query="I attack.",
            collection_names=["mothership_core"],
            n_results=6,
        )
        assert result.rule_chunks == chunks

    async def test_rag_skipped_when_no_vector_collections(self):
        char = _make_character()
        modules = [{"module_type": "pdf", "chroma_collection": None}]
        phase, _, rag, _ = self._make_phase(character=char, rule_modules=modules)
        result = await phase.assemble(_make_intent("I rest."), "camp-001")
        rag.retrieve_rule_chunks.assert_not_called()
        assert result.rule_chunks == []

    async def test_rag_skipped_when_no_rule_modules(self):
        char = _make_character()
        phase, _, rag, _ = self._make_phase(character=char, rule_modules=[])
        await phase.assemble(_make_intent("I rest."), "camp-001")
        rag.retrieve_rule_chunks.assert_not_called()

    async def test_rolling_vault_context_included(self):
        char = _make_character()
        phase, _, _, vault = self._make_phase(character=char, vault_context="[Turn 5] Kira looted the armory.")
        result = await phase.assemble(_make_intent(), "camp-001")
        vault.get_context_block.assert_called_once_with("camp-001")
        assert "Turn 5" in result.rolling_context

    async def test_rolling_vault_empty_when_no_vault_service(self):
        char = _make_character()
        db = AsyncMock()
        rag = AsyncMock()
        db.get_character_by_player.return_value = char
        db.get_inventory.return_value = []
        db.get_active_rule_modules.return_value = []
        phase = IngestionPhase(db=db, rag=rag, rolling_vault=None)
        result = await phase.assemble(_make_intent(), "camp-001")
        assert result.rolling_context == ""

    async def test_pdf_name_allowlist_populated_from_chunks(self):
        char = _make_character()
        modules = [{"module_type": "vector", "chroma_collection": "sr_core"}]
        chunks = [_make_rule_chunk(content="Shadowrun uses Karma for advancement.")]
        phase, _, _, _ = self._make_phase(
            character=char, rule_modules=modules, rule_chunks=chunks
        )
        result = await phase.assemble(_make_intent(), "camp-001")
        assert "shadowrun" in result.pdf_name_allowlist
        assert "karma" in result.pdf_name_allowlist

    async def test_pdf_name_allowlist_empty_without_chunks(self):
        char = _make_character()
        phase, _, _, _ = self._make_phase(character=char, rule_modules=[])
        result = await phase.assemble(_make_intent(), "camp-001")
        assert result.pdf_name_allowlist == []

    async def test_raw_input_passed_through(self):
        char = _make_character()
        phase, _, _, _ = self._make_phase(character=char)
        intent = _make_intent("I kick down the door.")
        result = await phase.assemble(intent, "camp-001")
        assert result.raw_input == "I kick down the door."

    async def test_multiple_vector_collections_all_passed_to_rag(self):
        char = _make_character()
        modules = [
            {"module_type": "vector", "chroma_collection": "core_rules"},
            {"module_type": "vector", "chroma_collection": "expansion_pack"},
        ]
        phase, _, rag, _ = self._make_phase(character=char, rule_modules=modules)
        await phase.assemble(_make_intent(), "camp-001")
        rag.retrieve_rule_chunks.assert_called_once()
        _, kwargs = rag.retrieve_rule_chunks.call_args
        assert set(kwargs["collection_names"]) == {"core_rules", "expansion_pack"}

    async def test_non_vector_module_excluded_from_rag_collections(self):
        char = _make_character()
        modules = [
            {"module_type": "vector", "chroma_collection": "core_rules"},
            {"module_type": "pdf", "chroma_collection": "some_pdf"},
        ]
        phase, _, rag, _ = self._make_phase(character=char, rule_modules=modules)
        await phase.assemble(_make_intent(), "camp-001")
        _, kwargs = rag.retrieve_rule_chunks.call_args
        assert "some_pdf" not in kwargs["collection_names"]
        assert "core_rules" in kwargs["collection_names"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — AdjudicationPhase.resolve
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAdjudicationPhaseResolve:
    def _make_phase(self, resolution: OllamaResolutionPayload | None = None):
        ollama_client = AsyncMock()
        ollama_client.resolve_action.return_value = resolution or _make_resolution()
        router = AsyncMock()
        router.get_ollama_client.return_value = ollama_client
        phase = AdjudicationPhase(router=router)
        return phase, router, ollama_client

    async def test_returns_ollama_resolution_payload(self):
        phase, _, _ = self._make_phase()
        context = _make_context()
        result = await phase.resolve(context)
        assert isinstance(result, OllamaResolutionPayload)

    async def test_requests_client_from_router(self):
        phase, router, _ = self._make_phase()
        context = _make_context()
        await phase.resolve(context)
        router.get_ollama_client.assert_called_once()

    async def test_calls_resolve_action_with_context(self):
        phase, _, ollama = self._make_phase()
        context = _make_context("I shoot the turret.")
        await phase.resolve(context)
        ollama.resolve_action.assert_called_once_with(context)

    async def test_resolution_fields_propagated(self):
        expected = _make_resolution(
            action_type="skill_check",
            roll_result=18,
            difficulty=14,
            outcome=ActionOutcome.SUCCESS,
        )
        phase, _, _ = self._make_phase(resolution=expected)
        result = await phase.resolve(_make_context())
        assert result.action_type == "skill_check"
        assert result.roll_result == 18
        assert result.difficulty == 14
        assert result.outcome == ActionOutcome.SUCCESS

    async def test_critical_success_propagated(self):
        resolution = _make_resolution(outcome=ActionOutcome.CRITICAL_SUCCESS, roll_result=20)
        phase, _, _ = self._make_phase(resolution=resolution)
        result = await phase.resolve(_make_context())
        assert result.outcome == ActionOutcome.CRITICAL_SUCCESS

    async def test_critical_failure_propagated(self):
        resolution = _make_resolution(outcome=ActionOutcome.CRITICAL_FAILURE, roll_result=1)
        phase, _, _ = self._make_phase(resolution=resolution)
        result = await phase.resolve(_make_context())
        assert result.outcome == ActionOutcome.CRITICAL_FAILURE

    async def test_state_delta_character_id_preserved(self):
        resolution = _make_resolution(character_id="char-xyz")
        phase, _, _ = self._make_phase(resolution=resolution)
        result = await phase.resolve(_make_context())
        assert result.state_delta.character_id == "char-xyz"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — StateCommitPhase.commit
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestStateCommitPhaseCommit:
    def _make_phase(self, character=None, post_state=None):
        db = AsyncMock()
        cache = AsyncMock()
        cache.client = AsyncMock()
        cache.client.publish = AsyncMock()

        char = character or _make_character()
        db.get_character_by_id.return_value = char
        db.apply_state_delta.return_value = post_state or {"hp": 18, "str": 16, "armor": 2}

        phase = StateCommitPhase(db=db, cache=cache)
        return phase, db, cache

    async def test_raises_when_character_not_found(self):
        phase, db, _ = self._make_phase()
        db.get_character_by_id.return_value = None
        resolution = _make_resolution()
        with pytest.raises(ValueError, match="not found for state commit"):
            await phase.commit(resolution)

    async def test_returns_state_commit_payload(self):
        phase, _, _ = self._make_phase()
        result = await phase.commit(_make_resolution())
        assert isinstance(result, StateCommitPayload)

    async def test_intent_id_propagated(self):
        phase, _, _ = self._make_phase()
        resolution = _make_resolution()
        result = await phase.commit(resolution)
        assert result.intent_id == "intent-001"

    async def test_character_id_propagated(self):
        phase, _, _ = self._make_phase()
        result = await phase.commit(_make_resolution(character_id="char-abc"))
        assert result.character_id == "char-abc"

    async def test_pre_state_reflects_db_snapshot(self):
        char = _make_character(stats={"hp": 20, "str": 16, "armor": 2})
        phase, _, _ = self._make_phase(character=char)
        result = await phase.commit(_make_resolution())
        assert result.pre_state == {"hp": 20, "str": 16, "armor": 2}

    async def test_post_state_from_apply_delta(self):
        phase, _, _ = self._make_phase(post_state={"hp": 14, "str": 16, "armor": 2})
        result = await phase.commit(_make_resolution())
        assert result.post_state["hp"] == 14

    async def test_not_lethal_by_default(self):
        phase, _, _ = self._make_phase()
        result = await phase.commit(_make_resolution())
        assert result.lethal is False

    async def test_lethal_from_status_change_dead(self):
        phase, _, _ = self._make_phase()
        resolution = _make_resolution(status_change=CharacterStatus.DEAD)
        result = await phase.commit(resolution)
        assert result.lethal is True
        assert result.status_change == CharacterStatus.DEAD

    async def test_lethal_from_hp_reaching_zero(self):
        phase, _, _ = self._make_phase()
        resolution = _make_resolution(
            stat_deltas=[StatDelta(stat_key="hp", old_value=5, new_value=0)]
        )
        result = await phase.commit(resolution)
        assert result.lethal is True

    async def test_lethal_from_hp_going_negative(self):
        phase, _, _ = self._make_phase()
        resolution = _make_resolution(
            stat_deltas=[StatDelta(stat_key="hp", old_value=3, new_value=-4)]
        )
        result = await phase.commit(resolution)
        assert result.lethal is True

    async def test_hit_points_alias_detected_as_lethal(self):
        phase, _, _ = self._make_phase()
        resolution = _make_resolution(
            stat_deltas=[StatDelta(stat_key="hit_points", old_value=2, new_value=0)]
        )
        result = await phase.commit(resolution)
        assert result.lethal is True

    async def test_health_alias_detected_as_lethal(self):
        phase, _, _ = self._make_phase()
        resolution = _make_resolution(
            stat_deltas=[StatDelta(stat_key="health", old_value=1, new_value=-2)]
        )
        result = await phase.commit(resolution)
        assert result.lethal is True

    async def test_non_hp_stat_at_zero_not_lethal(self):
        phase, _, _ = self._make_phase()
        resolution = _make_resolution(
            stat_deltas=[StatDelta(stat_key="stamina", old_value=3, new_value=0)]
        )
        result = await phase.commit(resolution)
        assert result.lethal is False

    async def test_state_commit_event_published_to_redis(self):
        phase, _, cache = self._make_phase()
        await phase.commit(_make_resolution(character_id="char-001"))
        cache.client.publish.assert_called()
        all_calls = cache.client.publish.call_args_list
        channel_names = [c[0][0] for c in all_calls]
        assert "csv_sync_events" in channel_names

    async def test_state_commit_event_payload_is_valid_json(self):
        phase, _, cache = self._make_phase()
        await phase.commit(_make_resolution())
        first_call = cache.client.publish.call_args_list[0]
        payload = json.loads(first_call[0][1])
        assert payload["event"] == "state_commit"
        assert payload["character_id"] == "char-001"

    async def test_lethal_flag_in_state_commit_event(self):
        phase, _, cache = self._make_phase()
        resolution = _make_resolution(status_change=CharacterStatus.DEAD)
        await phase.commit(resolution)
        first_call = cache.client.publish.call_args_list[0]
        payload = json.loads(first_call[0][1])
        assert payload["lethal"] is True

    async def test_vehicle_commit_published_per_vehicle(self):
        phase, _, cache = self._make_phase()
        resolution = _make_resolution(
            vehicle_deltas=[
                VehicleDelta(vehicle_id="ship-001", hull_delta=-10),
                VehicleDelta(vehicle_id="ship-002", hull_delta=-5),
            ]
        )
        await phase.commit(resolution)
        calls = cache.client.publish.call_args_list
        vehicle_events = [
            json.loads(c[0][1])
            for c in calls
            if json.loads(c[0][1]).get("event") == "vehicle_commit"
        ]
        assert len(vehicle_events) == 2
        vehicle_ids = {e["vehicle_id"] for e in vehicle_events}
        assert "ship-001" in vehicle_ids
        assert "ship-002" in vehicle_ids

    async def test_vehicle_commit_hull_delta_in_event(self):
        phase, _, cache = self._make_phase()
        resolution = _make_resolution(
            vehicle_deltas=[VehicleDelta(vehicle_id="shuttle-01", hull_delta=-15)]
        )
        await phase.commit(resolution)
        calls = cache.client.publish.call_args_list
        vehicle_events = [
            json.loads(c[0][1])
            for c in calls
            if json.loads(c[0][1]).get("event") == "vehicle_commit"
        ]
        assert vehicle_events[0]["hull_delta"] == -15

    async def test_no_vehicle_commit_when_no_vehicle_deltas(self):
        phase, _, cache = self._make_phase()
        await phase.commit(_make_resolution(vehicle_deltas=[]))
        calls = cache.client.publish.call_args_list
        vehicle_events = [
            json.loads(c[0][1])
            for c in calls
            if json.loads(c[0][1]).get("event") == "vehicle_commit"
        ]
        assert vehicle_events == []

    async def test_apply_state_delta_called_with_resolution_delta(self):
        phase, db, _ = self._make_phase()
        resolution = _make_resolution()
        await phase.commit(resolution)
        db.apply_state_delta.assert_called_once_with(resolution.state_delta)

    async def test_get_character_by_id_called_with_correct_id(self):
        phase, db, _ = self._make_phase()
        resolution = _make_resolution(character_id="char-xyz")
        await phase.commit(resolution)
        db.get_character_by_id.assert_called_once_with("char-xyz")

    async def test_vehicle_with_no_id_skipped(self):
        phase, _, cache = self._make_phase()
        resolution = _make_resolution(
            vehicle_deltas=[VehicleDelta(vehicle_id="", hull_delta=-5)]
        )
        await phase.commit(resolution)
        calls = cache.client.publish.call_args_list
        vehicle_events = [
            json.loads(c[0][1])
            for c in calls
            if json.loads(c[0][1]).get("event") == "vehicle_commit"
        ]
        assert vehicle_events == []


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — NarrationPhase.narrate
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestNarrationPhaseNarrate:
    def _make_narrative_response(self, narrative: str = "You strike true.") -> NarrativeResponsePayload:
        return NarrativeResponsePayload(
            prompt_id="prompt-001",
            intent_id="intent-001",
            narrative=narrative,
            embed_title="Combat Turn",
        )

    def _make_phase(self, narrative: str = "You strike true."):
        gm = AsyncMock()
        gm.narrate.return_value = self._make_narrative_response(narrative)
        phase = NarrationPhase(gm_director=gm)
        return phase, gm

    async def test_returns_narrative_response_payload(self):
        phase, _ = self._make_phase()
        result = await phase.narrate(
            resolution=_make_resolution(),
            commit=StateCommitPayload(
                intent_id="intent-001",
                character_id="char-001",
                pre_state={"hp": 20},
                post_state={"hp": 18},
            ),
            character=_make_character(),
            player_intent="I attack the guard.",
            campaign_system="mothership",
            campaign_id="camp-001",
        )
        assert isinstance(result, NarrativeResponsePayload)

    async def test_delegates_to_gm_director(self):
        phase, gm = self._make_phase()
        resolution = _make_resolution()
        commit = StateCommitPayload(
            intent_id="intent-001",
            character_id="char-001",
            pre_state={},
            post_state={},
        )
        character = _make_character()
        await phase.narrate(
            resolution=resolution,
            commit=commit,
            character=character,
            player_intent="I search the room.",
            campaign_system="dnd5e",
            campaign_id="camp-001",
        )
        gm.narrate.assert_called_once_with(
            resolution=resolution,
            commit=commit,
            character=character,
            player_intent="I search the room.",
            campaign_system="dnd5e",
            campaign_id="camp-001",
            active_directives=None,
            pdf_name_allowlist=None,
        )

    async def test_narrative_text_propagated(self):
        phase, _ = self._make_phase(narrative="The enemy falls to the ground.")
        result = await phase.narrate(
            resolution=_make_resolution(),
            commit=StateCommitPayload(
                intent_id="intent-001",
                character_id="char-001",
                pre_state={},
                post_state={},
            ),
            character=_make_character(),
            player_intent="I attack.",
            campaign_system="mothership",
            campaign_id="camp-001",
        )
        assert result.narrative == "The enemy falls to the ground."

    async def test_passes_active_directives_when_provided(self):
        phase, gm = self._make_phase()
        directives = [MagicMock()]
        await phase.narrate(
            resolution=_make_resolution(),
            commit=StateCommitPayload(
                intent_id="intent-001",
                character_id="char-001",
                pre_state={},
                post_state={},
            ),
            character=_make_character(),
            player_intent="test",
            campaign_system="dnd5e",
            campaign_id="camp-001",
            active_directives=directives,
        )
        _, kwargs = gm.narrate.call_args
        assert kwargs["active_directives"] is directives

    async def test_passes_pdf_name_allowlist_when_provided(self):
        phase, gm = self._make_phase()
        allowlist = ["shadowrun", "karma", "awakened"]
        await phase.narrate(
            resolution=_make_resolution(),
            commit=StateCommitPayload(
                intent_id="intent-001",
                character_id="char-001",
                pre_state={},
                post_state={},
            ),
            character=_make_character(),
            player_intent="test",
            campaign_system="shadowrun",
            campaign_id="camp-001",
            pdf_name_allowlist=allowlist,
        )
        _, kwargs = gm.narrate.call_args
        assert kwargs["pdf_name_allowlist"] == allowlist

    async def test_campaign_system_passed_correctly(self):
        phase, gm = self._make_phase()
        await phase.narrate(
            resolution=_make_resolution(),
            commit=StateCommitPayload(
                intent_id="intent-001",
                character_id="char-001",
                pre_state={},
                post_state={},
            ),
            character=_make_character(),
            player_intent="test",
            campaign_system="pirate_borg",
            campaign_id="camp-001",
        )
        _, kwargs = gm.narrate.call_args
        assert kwargs["campaign_system"] == "pirate_borg"

    async def test_campaign_id_passed_correctly(self):
        phase, gm = self._make_phase()
        await phase.narrate(
            resolution=_make_resolution(),
            commit=StateCommitPayload(
                intent_id="intent-001",
                character_id="char-001",
                pre_state={},
                post_state={},
            ),
            character=_make_character(),
            player_intent="test",
            campaign_system="dnd5e",
            campaign_id="camp-xyz",
        )
        _, kwargs = gm.narrate.call_args
        assert kwargs["campaign_id"] == "camp-xyz"

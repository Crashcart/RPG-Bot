"""
Tests for NatsBus — Multi-Agent Vector-Space Communication
==========================================================
All tests mock the NATS client — no live server required.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.nats_schemas import (
    CombatBoardEvent,
    EmotionHash,
    FogUpdateEvent,
    NpcReactionEvent,
    SceneStateVector,
    emotion_name,
)
from orchestrator.services.nats_bus import NatsBus


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def bus_disabled() -> NatsBus:
    """NatsBus with no URL configured (graceful degradation mode)."""
    return NatsBus(nats_url="")


@pytest.fixture
def mock_nc() -> MagicMock:
    nc = MagicMock()
    nc.drain = AsyncMock()
    js = MagicMock()
    js.publish   = AsyncMock()
    js.subscribe = AsyncMock()
    nc.jetstream.return_value = js
    return nc


@pytest.fixture
def bus_connected(mock_nc: MagicMock) -> NatsBus:
    """NatsBus already in connected state (bypasses nats.connect)."""
    bus = NatsBus(nats_url="nats://localhost:4222")
    bus._nc    = mock_nc
    bus._js    = mock_nc.jetstream()
    bus._ready = True
    return bus


@pytest.fixture
def sample_vector() -> SceneStateVector:
    return SceneStateVector(
        scene_id="abc12345",
        campaign_id="campaign-uuid-1",
        event_type="combat_start",
        emotion_hashes={"npc-1": EmotionHash.AGGRO, "npc-2": EmotionHash.FEAR},
        player_action_summary="Player draws sword",
        aggro_target="player-1",
        visible_to=[],
    )


@pytest.fixture
def sample_reaction() -> NpcReactionEvent:
    return NpcReactionEvent(
        npc_id="npc-1",
        campaign_id="campaign-uuid-1",
        scene_id="abc12345",
        emotion_hash=EmotionHash.AGGRO,
        target_id="player-1",
        intended_action="attack",
    )


# ─────────────────────────────────────────────────────────────────────────────
# EmotionHash Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEmotionHash:
    def test_all_hashes_have_names(self) -> None:
        for member in EmotionHash:
            assert emotion_name(member.value) == member.name.lower()

    def test_unknown_hash_returns_fallback(self) -> None:
        assert emotion_name(999) == "unknown(999)"

    def test_aggro_is_1(self) -> None:
        assert EmotionHash.AGGRO == 1

    def test_dead_is_9(self) -> None:
        assert EmotionHash.DEAD == 9

    def test_neutral_is_0(self) -> None:
        assert EmotionHash.NEUTRAL == 0


# ─────────────────────────────────────────────────────────────────────────────
# Graceful Degradation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_publish_scene_no_url(
        self, bus_disabled: NatsBus, sample_vector: SceneStateVector
    ) -> None:
        """publish_scene_state is a no-op when bus is disabled."""
        await bus_disabled.publish_scene_state(sample_vector)

    @pytest.mark.asyncio
    async def test_publish_reaction_no_url(
        self, bus_disabled: NatsBus, sample_reaction: NpcReactionEvent
    ) -> None:
        await bus_disabled.publish_npc_reaction(sample_reaction)

    @pytest.mark.asyncio
    async def test_subscribe_no_url(self, bus_disabled: NatsBus) -> None:
        async def _cb(event: NpcReactionEvent) -> None:
            pass
        await bus_disabled.subscribe_npc_reactions("campaign-1", _cb)

    @pytest.mark.asyncio
    async def test_connect_no_url(self, bus_disabled: NatsBus) -> None:
        await bus_disabled.connect()
        assert not bus_disabled.is_ready

    @pytest.mark.asyncio
    async def test_connect_server_down(self) -> None:
        """connect() gracefully disables the bus when server is unreachable."""
        bus = NatsBus(nats_url="nats://127.0.0.1:4999")
        with patch("nats.connect", side_effect=ConnectionRefusedError("refused")):
            await bus.connect()
        assert not bus.is_ready


# ─────────────────────────────────────────────────────────────────────────────
# SceneStateVector Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSceneStateVector:
    def test_visible_to_empty_is_global(self, sample_vector: SceneStateVector) -> None:
        assert sample_vector.visible_to == []

    def test_emotion_hashes_stored(self, sample_vector: SceneStateVector) -> None:
        assert sample_vector.emotion_hashes["npc-1"] == EmotionHash.AGGRO
        assert sample_vector.emotion_hashes["npc-2"] == EmotionHash.FEAR

    def test_player_action_truncated(self) -> None:
        long_action = "x" * 200
        vec = SceneStateVector(
            scene_id="s1",
            campaign_id="c1",
            event_type="tick",
            player_action_summary=long_action[:80],
        )
        assert len(vec.player_action_summary) <= 80

    def test_serialises_to_json(self, sample_vector: SceneStateVector) -> None:
        raw = sample_vector.model_dump_json()
        assert "campaign-uuid-1" in raw
        assert "combat_start" in raw


# ─────────────────────────────────────────────────────────────────────────────
# Publish Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPublishSceneState:
    @pytest.mark.asyncio
    async def test_global_broadcast_single_publish(
        self, bus_connected: NatsBus, sample_vector: SceneStateVector
    ) -> None:
        """Empty visible_to → one publish on the campaign subject."""
        await bus_connected.publish_scene_state(sample_vector)
        bus_connected._js.publish.assert_called_once()
        subject = bus_connected._js.publish.call_args[0][0]
        assert subject == "aetheris.scene.campaign-uuid-1"

    @pytest.mark.asyncio
    async def test_targeted_broadcast_per_entity(
        self, bus_connected: NatsBus, sample_vector: SceneStateVector
    ) -> None:
        """visible_to=[e1, e2] → two targeted publishes, one per entity."""
        sample_vector.visible_to = ["npc-1", "npc-2"]
        await bus_connected.publish_scene_state(sample_vector)
        assert bus_connected._js.publish.call_count == 2
        subjects = {c[0][0] for c in bus_connected._js.publish.call_args_list}
        assert "aetheris.scene.campaign-uuid-1.npc.npc-1" in subjects
        assert "aetheris.scene.campaign-uuid-1.npc.npc-2" in subjects

    @pytest.mark.asyncio
    async def test_publish_failure_is_silent(
        self, bus_connected: NatsBus, sample_vector: SceneStateVector
    ) -> None:
        bus_connected._js.publish.side_effect = RuntimeError("NATS error")
        await bus_connected.publish_scene_state(sample_vector)  # must not raise


class TestPublishNpcReaction:
    @pytest.mark.asyncio
    async def test_correct_subject(
        self, bus_connected: NatsBus, sample_reaction: NpcReactionEvent
    ) -> None:
        await bus_connected.publish_npc_reaction(sample_reaction)
        subject = bus_connected._js.publish.call_args[0][0]
        assert subject == "aetheris.npc.npc-1.react"

    @pytest.mark.asyncio
    async def test_payload_contains_emotion(
        self, bus_connected: NatsBus, sample_reaction: NpcReactionEvent
    ) -> None:
        await bus_connected.publish_npc_reaction(sample_reaction)
        raw_bytes = bus_connected._js.publish.call_args[0][1]
        data = json.loads(raw_bytes.decode())
        assert data["emotion_hash"] == EmotionHash.AGGRO


class TestPublishCombatBoard:
    @pytest.mark.asyncio
    async def test_combat_board_subject(self, bus_connected: NatsBus) -> None:
        event = CombatBoardEvent(
            campaign_id="camp-1",
            round_number=1,
            initiative_order=["npc-1", "player-1"],
            active_entity_id="npc-1",
        )
        await bus_connected.publish_combat_board(event)
        subject = bus_connected._js.publish.call_args[0][0]
        assert subject == "aetheris.combat.camp-1"


# ─────────────────────────────────────────────────────────────────────────────
# make_scene_vector Factory Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeSceneVector:
    def test_invalid_emotion_hash_becomes_neutral(self) -> None:
        vec = NatsBus.make_scene_vector(
            campaign_id="c1",
            event_type="tick",
            entity_emotions={"npc-1": 999},
        )
        assert vec.emotion_hashes["npc-1"] == EmotionHash.NEUTRAL

    def test_valid_emotions_preserved(self) -> None:
        vec = NatsBus.make_scene_vector(
            campaign_id="c1",
            event_type="combat_start",
            entity_emotions={"npc-1": EmotionHash.AGGRO, "npc-2": EmotionHash.FEAR},
        )
        assert vec.emotion_hashes["npc-1"] == EmotionHash.AGGRO
        assert vec.emotion_hashes["npc-2"] == EmotionHash.FEAR

    def test_action_truncated_to_80_chars(self) -> None:
        vec = NatsBus.make_scene_vector(
            campaign_id="c1",
            event_type="tick",
            entity_emotions={},
            player_action="x" * 200,
        )
        assert len(vec.player_action_summary) <= 80

    def test_visible_to_defaults_empty(self) -> None:
        vec = NatsBus.make_scene_vector(
            campaign_id="c1",
            event_type="tick",
            entity_emotions={},
        )
        assert vec.visible_to == []


# ─────────────────────────────────────────────────────────────────────────────
# Subscribe Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSubscribeNpcReactions:
    @pytest.mark.asyncio
    async def test_subscribe_registers_handler(self, bus_connected: NatsBus) -> None:
        async def _cb(event: NpcReactionEvent) -> None:
            pass
        await bus_connected.subscribe_npc_reactions("camp-1", _cb)
        bus_connected._js.subscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_malformed_message_dropped_silently(
        self, bus_connected: NatsBus
    ) -> None:
        """A malformed NPC reaction must not crash the pipeline."""
        received: list[NpcReactionEvent] = []

        async def _cb(event: NpcReactionEvent) -> None:
            received.append(event)

        await bus_connected.subscribe_npc_reactions("camp-1", _cb)
        handler_kwargs = bus_connected._js.subscribe.call_args[1]
        message_handler = handler_kwargs["cb"]

        msg = MagicMock()
        msg.data = b"not-json"
        msg.ack  = AsyncMock()
        await message_handler(msg)

        assert received == []

    @pytest.mark.asyncio
    async def test_wrong_campaign_message_filtered(
        self, bus_connected: NatsBus
    ) -> None:
        received: list[NpcReactionEvent] = []

        async def _cb(event: NpcReactionEvent) -> None:
            received.append(event)

        await bus_connected.subscribe_npc_reactions("camp-SUBSCRIBED", _cb)
        handler_kwargs = bus_connected._js.subscribe.call_args[1]
        message_handler = handler_kwargs["cb"]

        reaction = NpcReactionEvent(
            npc_id="npc-1",
            campaign_id="camp-OTHER",
            scene_id="s1",
            emotion_hash=EmotionHash.AGGRO,
            intended_action="attack",
        )
        msg = MagicMock()
        msg.data = reaction.model_dump_json().encode()
        msg.ack  = AsyncMock()
        await message_handler(msg)

        assert received == []

"""Unit tests for NatsBus and NATS event schemas.

All tests are fully mocked — no live NATS server is required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.nats_schemas import (
    CombatBoardEvent,
    FogUpdateEvent,
    NpcReactionEvent,
    SceneStateEvent,
)
from orchestrator.services.nats_bus import NatsBus


# ─────────────────────────────────────────────────────────────────────────────
# Schema tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSceneStateEvent:
    def test_defaults(self):
        evt = SceneStateEvent(
            scene_id="s1", campaign_id="c1", event_type="player_action",
            actor_id="player-42",
        )
        assert evt.visible_to == []
        assert evt.round_number == 0
        assert evt.payload == {}

    def test_visible_to_population(self):
        evt = SceneStateEvent(
            scene_id="s1", campaign_id="c1", event_type="env",
            actor_id="gm", visible_to=["npc-1", "npc-2"],
        )
        assert "npc-1" in evt.visible_to
        assert "npc-2" in evt.visible_to

    def test_round_trip_json(self):
        evt = SceneStateEvent(
            scene_id="abc", campaign_id="camp", event_type="combat",
            actor_id="goblin", payload={"hp": 5}, visible_to=["hero"],
            round_number=3,
        )
        restored = SceneStateEvent.model_validate_json(evt.model_dump_json())
        assert restored == evt


class TestNpcReactionEvent:
    def test_defaults(self):
        evt = NpcReactionEvent(
            npc_id="npc-1", scene_id="s1", campaign_id="c1",
            reaction_type="dialogue",
        )
        assert evt.dialogue == ""
        assert evt.action == {}

    def test_round_trip_json(self):
        evt = NpcReactionEvent(
            npc_id="goblin-a", scene_id="cave", campaign_id="camp-x",
            reaction_type="combat_action", dialogue="Raaargh!",
            action={"type": "attack", "target": "player"},
        )
        assert NpcReactionEvent.model_validate_json(evt.model_dump_json()) == evt


class TestCombatBoardEvent:
    def test_defaults(self):
        evt = CombatBoardEvent(scene_id="s", campaign_id="c", round_number=1)
        assert evt.board_state == {}
        assert evt.turn_order == []
        assert evt.active_combatant_id == ""

    def test_round_trip_json(self):
        evt = CombatBoardEvent(
            scene_id="dungeon", campaign_id="camp", round_number=2,
            board_state={"goblin": {"hp": 10}},
            turn_order=["player", "goblin"],
            active_combatant_id="goblin",
        )
        assert CombatBoardEvent.model_validate_json(evt.model_dump_json()) == evt


class TestFogUpdateEvent:
    def test_defaults(self):
        evt = FogUpdateEvent(scene_id="s", campaign_id="c", entity_id="hero")
        assert evt.x == 0.0
        assert evt.y == 0.0
        assert evt.visible_radius == 6.0
        assert evt.revealed_tiles == []

    def test_round_trip_json(self):
        evt = FogUpdateEvent(
            scene_id="map1", campaign_id="c", entity_id="hero",
            x=3.0, y=7.5, visible_radius=8.0,
            revealed_tiles=["(3,7)", "(3,8)", "(4,7)"],
        )
        assert FogUpdateEvent.model_validate_json(evt.model_dump_json()) == evt


# ─────────────────────────────────────────────────────────────────────────────
# NatsBus connection / graceful degradation tests
# ─────────────────────────────────────────────────────────────────────────────


class TestNatsBusGracefulDegradation:
    """NatsBus must not crash the pipeline when NATS is unavailable."""

    @pytest.mark.asyncio
    async def test_connect_failure_sets_disconnected(self):
        bus = NatsBus("nats://unreachable:4222")
        with patch("nats.connect", side_effect=OSError("Connection refused")):
            await bus.connect()
        assert bus.is_connected is False

    @pytest.mark.asyncio
    async def test_publish_scene_state_noop_when_disconnected(self):
        bus = NatsBus("nats://unreachable:4222")
        # No exception should be raised
        evt = SceneStateEvent(
            scene_id="s", campaign_id="c", event_type="env", actor_id="gm",
        )
        await bus.publish_scene_state(evt)

    @pytest.mark.asyncio
    async def test_publish_npc_reaction_noop_when_disconnected(self):
        bus = NatsBus("nats://unreachable:4222")
        evt = NpcReactionEvent(
            npc_id="npc-1", scene_id="s", campaign_id="c", reaction_type="idle",
        )
        await bus.publish_npc_reaction(evt)

    @pytest.mark.asyncio
    async def test_publish_combat_update_noop_when_disconnected(self):
        bus = NatsBus("nats://unreachable:4222")
        evt = CombatBoardEvent(scene_id="s", campaign_id="c", round_number=1)
        await bus.publish_combat_update(evt)

    @pytest.mark.asyncio
    async def test_publish_fog_update_noop_when_disconnected(self):
        bus = NatsBus("nats://unreachable:4222")
        evt = FogUpdateEvent(scene_id="s", campaign_id="c", entity_id="hero")
        await bus.publish_fog_update(evt)

    @pytest.mark.asyncio
    async def test_subscribe_scene_returns_none_when_disconnected(self):
        bus = NatsBus("nats://unreachable:4222")
        result = await bus.subscribe_scene("s1", callback=AsyncMock())
        assert result is None

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_never_connected(self):
        bus = NatsBus("nats://unreachable:4222")
        await bus.disconnect()  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# NatsBus connected path — mocked nats client
# ─────────────────────────────────────────────────────────────────────────────


def _make_connected_bus() -> tuple[NatsBus, MagicMock]:
    """Return a NatsBus with a mocked nats connection already wired in."""
    bus = NatsBus("nats://localhost:4222")
    mock_js = MagicMock()
    mock_js.publish = AsyncMock()
    mock_js.subscribe = AsyncMock(return_value=MagicMock())
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    mock_nc.drain = AsyncMock()
    bus._nc = mock_nc
    bus._js = mock_js
    bus._connected = True
    return bus, mock_js


class TestNatsBusConnectedPublish:
    @pytest.mark.asyncio
    async def test_publish_scene_state_public(self):
        bus, js = _make_connected_bus()
        evt = SceneStateEvent(
            scene_id="room1", campaign_id="c", event_type="env", actor_id="gm",
        )
        await bus.publish_scene_state(evt)
        js.publish.assert_awaited_once()
        subject, payload = js.publish.call_args[0]
        assert subject == "aetheris.scene.room1"
        assert b"room1" in payload

    @pytest.mark.asyncio
    async def test_publish_scene_state_private_epistemic_boundary(self):
        bus, js = _make_connected_bus()
        evt = SceneStateEvent(
            scene_id="room1", campaign_id="c", event_type="env", actor_id="gm",
            visible_to=["npc-a", "npc-b"],
        )
        await bus.publish_scene_state(evt)
        assert js.publish.await_count == 2  # one publish per recipient
        subjects = [call[0][0] for call in js.publish.call_args_list]
        assert "aetheris.scene.room1.for.npc-a" in subjects
        assert "aetheris.scene.room1.for.npc-b" in subjects

    @pytest.mark.asyncio
    async def test_publish_npc_reaction(self):
        bus, js = _make_connected_bus()
        evt = NpcReactionEvent(
            npc_id="goblin-1", scene_id="s", campaign_id="c",
            reaction_type="dialogue", dialogue="Surrender!",
        )
        await bus.publish_npc_reaction(evt)
        js.publish.assert_awaited_once()
        subject, payload = js.publish.call_args[0]
        assert subject == "aetheris.npc.goblin-1.react"
        assert b"Surrender!" in payload

    @pytest.mark.asyncio
    async def test_publish_combat_update(self):
        bus, js = _make_connected_bus()
        evt = CombatBoardEvent(scene_id="dungeon", campaign_id="c", round_number=4)
        await bus.publish_combat_update(evt)
        js.publish.assert_awaited_once()
        subject, _ = js.publish.call_args[0]
        assert subject == "aetheris.combat.dungeon"

    @pytest.mark.asyncio
    async def test_publish_fog_update(self):
        bus, js = _make_connected_bus()
        evt = FogUpdateEvent(scene_id="map2", campaign_id="c", entity_id="hero")
        await bus.publish_fog_update(evt)
        js.publish.assert_awaited_once()
        subject, _ = js.publish.call_args[0]
        assert subject == "aetheris.fog.map2"

    @pytest.mark.asyncio
    async def test_publish_scene_state_js_error_is_swallowed(self):
        bus, js = _make_connected_bus()
        js.publish = AsyncMock(side_effect=RuntimeError("NATS broker unavailable"))
        evt = SceneStateEvent(
            scene_id="s", campaign_id="c", event_type="env", actor_id="gm",
        )
        await bus.publish_scene_state(evt)  # must not raise


class TestNatsBusSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_scene_public(self):
        bus, js = _make_connected_bus()
        handle = await bus.subscribe_scene("room1", callback=AsyncMock())
        js.subscribe.assert_awaited_once()
        subject = js.subscribe.call_args[0][0]
        assert subject == "aetheris.scene.room1"
        assert handle is not None

    @pytest.mark.asyncio
    async def test_subscribe_scene_private_agent(self):
        bus, js = _make_connected_bus()
        await bus.subscribe_scene("room1", callback=AsyncMock(), agent_id="npc-x")
        subject = js.subscribe.call_args[0][0]
        assert subject == "aetheris.scene.room1.for.npc-x"

    @pytest.mark.asyncio
    async def test_subscribe_npc_reactions(self):
        bus, js = _make_connected_bus()
        await bus.subscribe_npc_reactions("goblin-1", callback=AsyncMock())
        subject = js.subscribe.call_args[0][0]
        assert subject == "aetheris.npc.goblin-1.react"

    @pytest.mark.asyncio
    async def test_subscribe_error_returns_none(self):
        bus, js = _make_connected_bus()
        js.subscribe = AsyncMock(side_effect=RuntimeError("broker error"))
        result = await bus.subscribe_scene("s", callback=AsyncMock())
        assert result is None


class TestNatsBusLifecycle:
    @pytest.mark.asyncio
    async def test_connect_success_sets_connected(self):
        bus = NatsBus("nats://localhost:4222")
        mock_nc = MagicMock()
        mock_nc.jetstream = MagicMock(return_value=MagicMock())
        with patch("nats.connect", return_value=mock_nc) as mock_connect:
            # nats.connect is a coroutine in real nats-py
            import asyncio
            mock_connect.return_value = mock_nc
            mock_connect.side_effect = None
            # Patch the connect to return immediately
            async def fake_connect(*a, **kw):
                return mock_nc
            with patch("nats.connect", fake_connect):
                await bus.connect()
        assert bus.is_connected is True

    @pytest.mark.asyncio
    async def test_disconnect_drains_connection(self):
        bus, _ = _make_connected_bus()
        await bus.disconnect()
        bus._nc.drain.assert_awaited_once()
        assert bus.is_connected is False

    @pytest.mark.asyncio
    async def test_on_disconnect_callback_sets_flag(self):
        bus, _ = _make_connected_bus()
        await bus._on_disconnect()
        assert bus.is_connected is False

    @pytest.mark.asyncio
    async def test_on_reconnect_callback_sets_flag(self):
        bus = NatsBus("nats://localhost:4222")
        bus._connected = False
        await bus._on_reconnect()
        assert bus.is_connected is True

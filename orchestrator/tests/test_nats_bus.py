"""Unit tests for NatsBus — all NATS I/O is mocked."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.schemas.nats_schemas import (
    AggroState,
    CombatBoardEvent,
    FogUpdateEvent,
    NpcReactionEvent,
    SceneStateEvent,
)
from orchestrator.services.nats_bus import NatsBus


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _settings(jetstream: bool = True):
    s = MagicMock()
    s.nats_url = "nats://localhost:4222"
    s.nats_jetstream_enabled = jetstream
    return s


def _connected_bus(settings=None, *, jetstream: bool = True) -> NatsBus:
    bus = NatsBus(settings or _settings(jetstream))
    bus._connected = True
    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    mock_js = AsyncMock()
    bus._nc = mock_nc
    bus._js = mock_js if jetstream else None
    return bus


# ── Connection tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_success():
    mock_nc = AsyncMock()
    mock_js_ctx = AsyncMock()
    mock_nc.jetstream.return_value = mock_js_ctx
    mock_js_ctx.find_stream = AsyncMock(return_value=MagicMock())

    with patch("orchestrator.services.nats_bus.nats.connect", return_value=mock_nc):
        bus = NatsBus(_settings())
        await bus.connect()

    assert bus.connected is True


@pytest.mark.asyncio
async def test_connect_failure_degrades_gracefully():
    with patch(
        "orchestrator.services.nats_bus.nats.connect",
        side_effect=ConnectionRefusedError("unreachable"),
    ):
        bus = NatsBus(_settings())
        await bus.connect()

    assert bus.connected is False


@pytest.mark.asyncio
async def test_disconnect_drains_connection():
    bus = _connected_bus()
    await bus.disconnect()
    bus._nc.drain.assert_awaited_once()
    assert bus.connected is False


# ── Publish tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_uses_jetstream_when_enabled():
    bus = _connected_bus(jetstream=True)
    await bus.publish("aetheris.scene.abc", {"key": "value"})
    bus._js.publish.assert_awaited_once()
    call_args = bus._js.publish.call_args
    assert call_args[0][0] == "aetheris.scene.abc"
    assert json.loads(call_args[0][1]) == {"key": "value"}


@pytest.mark.asyncio
async def test_publish_uses_core_nats_when_jetstream_disabled():
    bus = _connected_bus(jetstream=False)
    bus._js = None
    await bus.publish("aetheris.scene.abc", {"key": "value"})
    bus._nc.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_noop_when_not_connected():
    bus = NatsBus(_settings())
    bus._connected = False
    # Should not raise
    await bus.publish("any.subject", {"key": "val"})


# ── subscribe tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_registers_jetstream_durable():
    bus = _connected_bus(jetstream=True)
    cb = AsyncMock()
    await bus.subscribe("aetheris.scene.abc", cb, durable="test-consumer")
    bus._js.subscribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscribe_noop_when_not_connected():
    bus = NatsBus(_settings())
    bus._connected = False
    cb = AsyncMock()
    await bus.subscribe("any.subject", cb)
    # No exception, cb never called
    cb.assert_not_awaited()


# ── Schema tests ──────────────────────────────────────────────────────────────


def test_scene_state_event_serialisation():
    event = SceneStateEvent(
        campaign_id="camp-1",
        turn_id="t-001",
        location="The Frozen Tundra",
        narrative_summary="A blizzard howls.",
        atmosphere_vector={"tension": 0.8, "fear": 0.4},
    )
    data = json.loads(event.model_dump_json())
    assert data["campaign_id"] == "camp-1"
    assert data["atmosphere_vector"]["tension"] == 0.8
    assert data["visible_to"] is None


def test_npc_reaction_event_defaults():
    event = NpcReactionEvent(
        npc_id="goblin-1",
        campaign_id="camp-1",
        turn_id="t-001",
        scene_summary="The player drew a sword.",
    )
    assert event.aggro_state == AggroState.PASSIVE
    assert event.fear_level == 0
    assert event.known_facts == []


def test_combat_board_event_serialisation():
    event = CombatBoardEvent(
        campaign_id="camp-1",
        turn_id="t-002",
        round_number=3,
        participants=[
            {"entity_id": "player-1", "hp": 24, "position": [2, 4], "action_available": True}
        ],
    )
    data = event.model_dump()
    assert data["round_number"] == 3
    assert data["participants"][0]["hp"] == 24


def test_fog_update_event_tile_deltas():
    event = FogUpdateEvent(
        campaign_id="camp-1",
        turn_id="t-003",
        tile_deltas=[(0, 0, True), (1, 0, True), (2, 1, False)],
        map_width=10,
        map_height=10,
    )
    assert len(event.tile_deltas) == 3
    assert event.tile_deltas[2] == (2, 1, False)


# ── Domain helper tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_scene_state_broadcasts_and_fans_out():
    bus = _connected_bus(jetstream=True)
    bus.publish = AsyncMock()

    event = SceneStateEvent(
        campaign_id="camp-1",
        turn_id="t-001",
        location="Tavern",
        narrative_summary="A fight breaks out.",
    )
    await bus.publish_scene_state(event, npc_ids=["goblin-1", "goblin-2"])

    assert bus.publish.call_count == 3  # 1 scene + 2 NPC fan-outs
    subjects = [c.args[0] for c in bus.publish.call_args_list]
    assert "aetheris.scene.camp-1" in subjects
    assert "aetheris.npc.goblin-1.react" in subjects
    assert "aetheris.npc.goblin-2.react" in subjects


@pytest.mark.asyncio
async def test_publish_scene_state_respects_visible_to():
    """NPCs not in visible_to must not receive the NpcReactionEvent."""
    bus = _connected_bus(jetstream=True)
    bus.publish = AsyncMock()

    event = SceneStateEvent(
        campaign_id="camp-1",
        turn_id="t-001",
        location="Dungeon",
        narrative_summary="A secret door slides open.",
        visible_to=["goblin-1"],  # goblin-2 is out of sensory radius
    )
    await bus.publish_scene_state(event, npc_ids=["goblin-1", "goblin-2"])

    subjects = [c.args[0] for c in bus.publish.call_args_list]
    assert "aetheris.npc.goblin-1.react" in subjects
    assert "aetheris.npc.goblin-2.react" not in subjects


@pytest.mark.asyncio
async def test_publish_combat_board():
    bus = _connected_bus(jetstream=True)
    bus.publish = AsyncMock()

    event = CombatBoardEvent(
        campaign_id="camp-1",
        turn_id="t-005",
        round_number=1,
    )
    await bus.publish_combat_board(event)
    bus.publish.assert_awaited_once_with(
        "aetheris.combat.camp-1", event.model_dump()
    )


@pytest.mark.asyncio
async def test_publish_fog_update():
    bus = _connected_bus(jetstream=True)
    bus.publish = AsyncMock()

    event = FogUpdateEvent(
        campaign_id="camp-1",
        turn_id="t-006",
        tile_deltas=[(3, 3, True)],
        map_width=20,
        map_height=20,
    )
    await bus.publish_fog_update(event)
    bus.publish.assert_awaited_once_with(
        "aetheris.fog.camp-1", event.model_dump()
    )

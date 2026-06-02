"""Pydantic schemas for NATS inter-agent events.

All payloads are serialised as JSON before publishing and deserialised on
receipt.  The subject hierarchy is:

    aetheris.scene.{campaign_id}       — full scene state after every pipeline turn
    aetheris.npc.{npc_id}.react        — NPC reaction request
    aetheris.combat.{campaign_id}      — combat board broadcast
    aetheris.fog.{campaign_id}         — fog-of-war tile delta
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AggroState(int, Enum):
    PASSIVE = 0
    WARY = 1
    HOSTILE = 2
    FLEEING = 3


class SceneStateEvent(BaseModel):
    """Published to ``aetheris.scene.{campaign_id}`` after every pipeline turn."""

    campaign_id: str
    turn_id: str
    location: str
    narrative_summary: str
    atmosphere_vector: dict[str, float] = Field(
        default_factory=dict,
        description="Low-dimensional background atmosphere (tension, fear, chaos …)",
    )
    # Which NPC IDs are allowed to receive this event (None = broadcast to all)
    visible_to: list[str] | None = None


class NpcReactionEvent(BaseModel):
    """Published to ``aetheris.npc.{npc_id}.react`` to trigger an NPC response."""

    npc_id: str
    campaign_id: str
    turn_id: str
    scene_summary: str
    aggro_state: AggroState = AggroState.PASSIVE
    fear_level: int = Field(default=0, ge=0, le=10)
    sensory_radius_ft: int = 30
    # Mechanical facts the NPC is allowed to know
    known_facts: list[str] = Field(default_factory=list)


class CombatBoardEvent(BaseModel):
    """Broadcast to ``aetheris.combat.{campaign_id}`` for simultaneous NPC resolution."""

    campaign_id: str
    turn_id: str
    round_number: int
    participants: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of {entity_id, hp, position, action_available} dicts",
    )
    board_state: dict[str, Any] = Field(default_factory=dict)


class FogUpdateEvent(BaseModel):
    """Published to ``aetheris.fog.{campaign_id}`` when explored tiles change."""

    campaign_id: str
    turn_id: str
    # Delta: list of (col, row, revealed: bool) tuples
    tile_deltas: list[tuple[int, int, bool]] = Field(default_factory=list)
    map_width: int
    map_height: int

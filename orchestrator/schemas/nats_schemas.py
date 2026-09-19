"""NATS JetStream event schemas — inter-service message contracts.

Subject hierarchy:
  aetheris.scene.{scene_id}       — GM broadcasts scene state to all subscribers
  aetheris.npc.{npc_id}.react     — NPC agent publishes its reaction
  aetheris.combat.{scene_id}      — GM broadcasts combat board state
  aetheris.fog.{scene_id}         — GM publishes Fog-of-War delta for a scene
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SceneStateEvent(BaseModel):
    """Broadcast from GM to all subscribed agents when the scene changes."""

    scene_id: str
    campaign_id: str
    event_type: str  # e.g. "player_action", "npc_turn", "environment"
    actor_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # Epistemic boundary: only agents whose ID appears here receive this event.
    # An empty list means the event is public (visible to all agents).
    visible_to: list[str] = Field(default_factory=list)
    round_number: int = 0


class NpcReactionEvent(BaseModel):
    """Published by an NPC agent in response to a SceneStateEvent."""

    npc_id: str
    scene_id: str
    campaign_id: str
    reaction_type: str  # e.g. "dialogue", "combat_action", "flee", "idle"
    dialogue: str = ""
    action: dict[str, Any] = Field(default_factory=dict)


class CombatBoardEvent(BaseModel):
    """Snapshot of the full combat state broadcast by the GM after each turn."""

    scene_id: str
    campaign_id: str
    round_number: int
    # Serialised board: combatant IDs → HP, position, status
    board_state: dict[str, Any] = Field(default_factory=dict)
    active_combatant_id: str = ""
    turn_order: list[str] = Field(default_factory=list)


class FogUpdateEvent(BaseModel):
    """Incremental Fog-of-War delta published after a movement or reveal action."""

    scene_id: str
    campaign_id: str
    entity_id: str
    x: float = 0.0
    y: float = 0.0
    visible_radius: float = 6.0  # in grid units
    # Set of tile coordinates newly revealed by this update — format "(x,y)"
    revealed_tiles: list[str] = Field(default_factory=list)

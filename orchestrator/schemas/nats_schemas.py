"""
Ironclad GM – NATS Multi-Agent Vector-Space Communication Schemas
=================================================================
Pydantic models for the inter-agent message bus (issue #8).

Subject hierarchy:
  aetheris.scene.{campaign_id}        — SceneStateVector broadcast (GM → NPCs)
  aetheris.npc.{npc_id}.react         — NpcReactionEvent (NPC → GM)
  aetheris.combat.{campaign_id}       — CombatBoardEvent (GM → all combatants)
  aetheris.fog.{campaign_id}          — FogUpdateEvent (GM → map layer)

Epistemic boundaries are enforced at publish time:
  SceneStateVector.visible_to lists the entity IDs that may receive a given
  broadcast.  The NatsBus filters subscription delivery by this list so NPC
  agents never receive information outside their sensory radius.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Emotion / Intent Hash Table (4-byte integer IDs per TDR §3)
# ─────────────────────────────────────────────────────────────────────────────

class EmotionHash(IntEnum):
    """
    Lightweight integer emotion/intent dictionary.

    NPC agents receive these hashes instead of full English descriptions,
    bypassing tokenization overhead and preventing prompt injection via
    forged emotion strings.  Callers convert to/from names via emotion_name().
    """
    NEUTRAL    = 0   # default, no strong emotion
    AGGRO      = 1   # hostile/attacking
    FEAR       = 2   # frightened, fleeing
    CURIOUS    = 3   # investigating
    SUSPICIOUS = 4   # on alert, not yet hostile
    FRIENDLY   = 5   # allied or neutral-positive
    PANIC      = 6   # uncontrolled fear (routing, screaming)
    GUARD      = 7   # ordered defensive posture
    INJURED    = 8   # wounded, impaired
    DEAD       = 9   # no further processing
    CHARMED    = 10  # under magical compulsion
    STUNNED    = 11  # temporarily incapacitated


def emotion_name(hash_value: int) -> str:
    """Return the human-readable label for an EmotionHash integer value."""
    try:
        return EmotionHash(hash_value).name.lower()
    except ValueError:
        return f"unknown({hash_value})"


# ─────────────────────────────────────────────────────────────────────────────
# Scene State Vector (GM → NPC broadcast)
# ─────────────────────────────────────────────────────────────────────────────

class SceneStateVector(BaseModel):
    """
    Compressed scene state broadcast over aetheris.scene.{campaign_id}.

    The GM Director publishes this after every action resolution.  Each NPC
    agent receives the vector and updates its internal intent model without
    needing to re-parse a full English scene description — the emotion hashes
    are injected directly into its attention context.

    visible_to controls epistemic boundaries: only the listed entity IDs
    receive this broadcast.  An empty list means the vector is global (all
    campaign NPCs receive it).
    """
    scene_id:       str = Field(..., description="Unique ID for this scene state snapshot")
    campaign_id:    str = Field(..., description="Campaign UUID")
    event_type:     str = Field(
        ...,
        description="combat_start | npc_attacked | player_fled | social_approach | "
                    "item_used | zone_entered | zone_exited | tick",
    )
    emotion_hashes: dict[str, int] = Field(
        default_factory=dict,
        description="entity_id → EmotionHash integer.  Only entities whose state "
                    "changed this turn are included — unchanged entities are omitted "
                    "to minimise payload size.",
    )
    aggro_target:      str | None = Field(
        default=None,
        description="entity_id of the current aggro target, if any.",
    )
    player_action_summary: str = Field(
        default="",
        description="Terse (≤80 char) summary of the player's action, injected "
                    "into each NPC's context window.",
        max_length=80,
    )
    position_deltas: dict[str, list[int]] = Field(
        default_factory=dict,
        description="entity_id → [dx, dy] coordinate delta.  Omit entities "
                    "that did not move this tick.",
    )
    visible_to: list[str] = Field(
        default_factory=list,
        description="entity_ids allowed to receive this broadcast (epistemic "
                    "boundary).  Empty = broadcast to all campaign NPCs.",
    )
    published_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# NPC Reaction Event (NPC → GM)
# ─────────────────────────────────────────────────────────────────────────────

class NpcReactionEvent(BaseModel):
    """
    Published by an NPC agent after processing a SceneStateVector.

    Subject: aetheris.npc.{npc_id}.react

    The GM Director subscribes to all NPC reaction subjects for the active
    campaign and uses the emotion hashes to update npc_entity_state in
    PostgreSQL without invoking an LLM for state bookkeeping.
    """
    npc_id:       str = Field(..., description="NPC entity UUID")
    campaign_id:  str = Field(..., description="Campaign UUID")
    scene_id:     str = Field(..., description="scene_id of the vector being reacted to")
    emotion_hash: int = Field(
        ...,
        description="NPC's new EmotionHash after processing the scene vector",
    )
    target_id:    str | None = Field(
        default=None,
        description="entity_id of the NPC's current aggro/attention target",
    )
    intended_action: str = Field(
        default="",
        description="Short intent string: attack | flee | hide | call_for_help | idle",
        max_length=40,
    )
    reacted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Combat Board Event (GM → all combatants, parallel resolution)
# ─────────────────────────────────────────────────────────────────────────────

class CombatBoardEvent(BaseModel):
    """
    Broadcasts the full combat board state to all combatants simultaneously.

    Subject: aetheris.combat.{campaign_id}

    NPC agents receive this and calculate their optimal move in parallel
    (hive-mind pattern from the TDR).  The GM Director collects all
    NpcReactionEvent replies and resolves conflicts atomically.
    """
    campaign_id:      str = Field(..., description="Campaign UUID")
    round_number:     int = Field(..., ge=1, description="Current combat round")
    initiative_order: list[str] = Field(
        ...,
        description="entity_ids in initiative order (highest first)",
    )
    entity_stats: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="entity_id → minimal stat snapshot: {hp, max_hp, position, "
                    "status_effects}.  Only fields required for tactical decisions.",
    )
    active_entity_id: str = Field(
        ...,
        description="entity_id whose turn it currently is",
    )
    visible_to: list[str] = Field(
        default_factory=list,
        description="Epistemic filter: which combatants receive this board state. "
                    "Typically all active combatants.",
    )
    broadcast_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fog Update Event (GM → map layer)
# ─────────────────────────────────────────────────────────────────────────────

class FogUpdateEvent(BaseModel):
    """
    Signals that the Fog of War / LoS bitmask has changed for a campaign.

    Subject: aetheris.fog.{campaign_id}

    A separate map renderer service (if deployed) subscribes to this subject
    and updates the rendered map PNG in media-assets.  The orchestrator does
    not need to block on map rendering; this is fire-and-forget.
    """
    campaign_id:    str = Field(..., description="Campaign UUID")
    revealed_tiles: list[list[int]] = Field(
        default_factory=list,
        description="List of [x, y] tile coordinates newly revealed this turn",
    )
    hidden_tiles: list[list[int]] = Field(
        default_factory=list,
        description="List of [x, y] tile coordinates newly hidden this turn",
    )
    entity_positions: dict[str, list[int]] = Field(
        default_factory=dict,
        description="entity_id → [x, y] current position for token rendering",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

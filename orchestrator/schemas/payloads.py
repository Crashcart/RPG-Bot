"""
Ironclad GM – Canonical API Payload Schemas
============================================
All inter-service JSON payloads are defined here as Pydantic models.
These are the single source of truth for data contracts across the pipeline.

Pipeline flow:
  Discord ──► IntentPayload
           ──► ContextAssemblyPayload  (Phase 1: Ingestion)
           ──► OllamaResolutionPayload (Phase 2: Mechanical Adjudication)
           ──► StateCommitPayload      (Phase 3: State Commitment)
           ──► NarrativeRequestPayload (Phase 4: Narrative Generation)
           ──► NarrativeResponsePayload ──► Discord
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Shared Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class CommandType(str, Enum):
    ACTION        = "action"          # free-text narrative action
    SLASH_COMMAND = "slash_command"   # Discord slash command
    OOC           = "ooc"             # out-of-character meta message


class CharacterStatus(str, Enum):
    ALIVE   = "ALIVE"
    DEAD    = "DEAD"
    RETIRED = "RETIRED"


class ActionOutcome(str, Enum):
    CRITICAL_SUCCESS = "critical_success"
    SUCCESS          = "success"
    PARTIAL_SUCCESS  = "partial_success"
    FAILURE          = "failure"
    CRITICAL_FAILURE = "critical_failure"


class MultimediaType(str, Enum):
    IMAGE     = "image"
    SOUND_CUE = "sound_cue"
    AMBIENT   = "ambient"


class OperationalStatus(str, Enum):
    OPERATIONAL = "OPERATIONAL"
    DAMAGED     = "DAMAGED"
    DESTROYED   = "DESTROYED"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 – Ingestion Schema: Discord → Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class SlashCommandData(BaseModel):
    """Structured data for Discord slash commands."""
    command_name: str
    options: dict[str, Any] = Field(default_factory=dict)


class IntentPayload(BaseModel):
    """
    Constructed by the Discord listener on every player interaction.
    This is the entry point for the four-phase pipeline.
    """
    intent_id:     str  = Field(default_factory=lambda: str(uuid.uuid4()))
    player_id:     str  = Field(..., description="Discord user snowflake ID")
    guild_id:      str  = Field(..., description="Discord server snowflake ID")
    channel_id:    str  = Field(..., description="Discord channel snowflake ID")
    session_token: str  = Field(..., description="Redis session key (UUID)")
    raw_input:     str  = Field(..., description="Verbatim player input text")
    command_type:  CommandType = Field(default=CommandType.ACTION)
    slash_command: SlashCommandData | None = Field(
        default=None,
        description="Populated only when command_type == slash_command",
    )
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"json_schema_extra": {
        "example": {
            "intent_id":     "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "player_id":     "123456789012345678",
            "guild_id":      "987654321098765432",
            "channel_id":    "111222333444555666",
            "session_token": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "raw_input":     "I draw my sword and attack the goblin to my left.",
            "command_type":  "action",
            "slash_command": None,
            "timestamp":     "2024-01-15T20:30:00Z",
        }
    }}


class CharacterSnapshot(BaseModel):
    """Lightweight character state extracted from DB for context assembly."""
    character_id: str
    name:         str
    system:       str
    status:       CharacterStatus
    stats:        dict[str, Any]


class RuleChunk(BaseModel):
    """A single retrieved chunk from the vector rulebook store."""
    chunk_id:    str
    source:      str   # e.g. "PHB p.194", "Cyberpunk 2020 Core Rulebook p.88"
    content:     str
    relevance:   float = Field(ge=0.0, le=1.0)


class SubsystemSnapshot(BaseModel):
    """Mechanical state of a single vehicle component."""
    subsystem_id:          str
    subsystem_name:        str
    subsystem_type:        str
    operational_status:    OperationalStatus
    assigned_character_id: str | None = None
    subsystem_data:        dict[str, Any] = Field(default_factory=dict)


class VehicleSnapshot(BaseModel):
    """
    Mechanical state of a vehicle and all its subsystems.
    Included in ContextAssemblyPayload when vehicles are part of the scene.
    """
    vehicle_id:         str
    name:               str
    asset_type:         str
    hull_integrity:     int
    max_hull_integrity: int
    asset_data:         dict[str, Any]            = Field(default_factory=dict)
    subsystems:         list[SubsystemSnapshot]   = Field(default_factory=list)


class ContextAssemblyPayload(BaseModel):
    """
    Phase 1 output – fed into the Ollama mechanical engine.
    Contains intent, full character state, and retrieved rulebook context.
    """
    intent_id:          str
    character:          CharacterSnapshot
    inventory_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    vehicle_context:    list[dict[str, Any]] = Field(
        default_factory=list,
        description="Vehicle/asset snapshots for vehicles in the active scene.",
    )
    rule_chunks:        list[RuleChunk]      = Field(default_factory=list)
    raw_input:          str
    assembled_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 – Mechanical Adjudication Schema: Ollama → Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class DiceRequest(BaseModel):
    """
    When Ollama requires a true-RNG dice roll the backend generates it
    and injects the result back before state commitment.
    """
    notation:  str   = Field(..., description="Standard dice notation, e.g. '1d20', '2d6+3'")
    modifier:  int   = Field(default=0)
    purpose:   str   = Field(default="", description="Why this roll is being requested")


class StatDelta(BaseModel):
    """A single numeric stat change on a character."""
    stat_key:   str
    old_value:  Any
    new_value:  Any


class SubsystemDelta(BaseModel):
    """
    A single subsystem change within a vehicle_delta.
    Only non-null fields are applied; use '__no_change__' sentinel for
    assigned_character_id to leave the current assignment intact.
    """
    subsystem_name:        str
    new_status:            OperationalStatus | None = None
    assigned_character_id: str | None = "__no_change__"
    # "__no_change__" → leave assignment as-is
    # None            → unassign (clear the seat)
    # "<uuid>"        → assign this character


class VehicleDelta(BaseModel):
    """
    Changes to a vehicle's hull and subsystems from a single action.
    hull_delta is a signed integer: negative = damage, positive = repair.
    """
    vehicle_id:  str
    hull_delta:  int               = 0
    subsystems:  list[SubsystemDelta] = Field(default_factory=list)


class StateDelta(BaseModel):
    """
    The set of character state changes produced by mechanical resolution.
    Applied atomically to PostgreSQL in Phase 3.
    """
    character_id:   str
    stat_deltas:    list[StatDelta]          = Field(default_factory=list)
    status_change:  CharacterStatus | None   = Field(
        default=None,
        description="Non-null only when character status changes (e.g. ALIVE → DEAD)",
    )
    inventory_delta: list[dict[str, Any]]   = Field(
        default_factory=list,
        description="Items added (positive qty) or removed (negative qty). "
                    "Full JSONB payload is duplicated into the inventories table as-is.",
    )
    vehicle_deltas: list[VehicleDelta]      = Field(
        default_factory=list,
        description="Hull and subsystem mutations for any vehicles involved in this action.",
    )


class OllamaResolutionPayload(BaseModel):
    """
    Phase 2 output – the strictly mechanical result from the local LLM.
    Narrative generation is explicitly forbidden from this payload.
    """
    resolution_id:      str  = Field(default_factory=lambda: str(uuid.uuid4()))
    intent_id:          str
    action_type:        str  = Field(..., description="e.g. 'melee_attack', 'skill_check', 'saving_throw'")
    difficulty:         int  = Field(..., ge=1, description="Target number / DC")
    dice_request:       DiceRequest
    roll_result:        int  = Field(..., description="Final total after modifiers")
    outcome:            ActionOutcome
    state_delta:        StateDelta
    rulebook_citations: list[str]  = Field(default_factory=list)
    reasoning:          str        = Field(
        default="",
        description="Terse mechanical justification (no narrative flavor)",
    )
    resolved_at:        datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("reasoning")
    @classmethod
    def reasoning_must_be_mechanical(cls, v: str) -> str:
        """Guard: strip any narrative adjectives that slip through."""
        # In production this would run a lightweight classifier;
        # here we enforce a maximum length for raw justification text.
        if len(v) > 500:
            return v[:500]
        return v

    model_config = {"json_schema_extra": {
        "example": {
            "resolution_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "intent_id":     "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "action_type":   "melee_attack",
            "difficulty":    15,
            "dice_request":  {"notation": "1d20", "modifier": 3, "purpose": "attack roll"},
            "roll_result":   18,
            "outcome":       "success",
            "state_delta": {
                "character_id": "c1d2e3f4-0000-0000-0000-000000000001",
                "stat_deltas": [
                    {"stat_key": "target_hp", "old_value": 20, "new_value": 12}
                ],
                "status_change": None,
                "inventory_delta": [],
            },
            "rulebook_citations": ["PHB p.194 – Melee Attack"],
            "reasoning": "Attack roll 18 meets AC 15. Damage: 1d8+3 = 8.",
        }
    }}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 – State Commitment Schema
# ─────────────────────────────────────────────────────────────────────────────

class StateCommitPayload(BaseModel):
    """
    Phase 3 – Records the before/after DB state transition.
    Emitted after the PostgreSQL write succeeds; consumed by the CSV sync worker.
    """
    commit_id:      str      = Field(default_factory=lambda: str(uuid.uuid4()))
    intent_id:      str
    character_id:   str
    pre_state:      dict[str, Any]
    post_state:     dict[str, Any]
    status_change:  CharacterStatus | None = None
    lethal:         bool     = Field(
        default=False,
        description="True when this commit results in character death",
    )
    committed_at:   datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Story Memory Schemas (used in Phase 4 for continuity)
# ─────────────────────────────────────────────────────────────────────────────

class StoryEntityType(str, Enum):
    NPC          = "npc"
    LOCATION     = "location"
    EVENT        = "event"
    WORLD_FACT   = "world_fact"
    PLOT_THREAD  = "plot_thread"


class StoryFact(BaseModel):
    """
    A single established world fact retrieved from the story memory store.
    Injected verbatim into the Gemini prompt so the narrator cannot contradict it.
    """
    fact_id:       str
    entity_type:   StoryEntityType
    entity_name:   str
    summary:       str   = Field(..., description="One-sentence fact the GM must honour")
    detail:        str   = Field(default="", description="Extended context")
    relevance:     float = Field(default=1.0, ge=0.0, le=1.0)
    established_at: datetime


class ExtractedFact(BaseModel):
    """Schema for a single fact extracted from a generated narrative by Gemini."""
    entity_type:  StoryEntityType
    entity_name:  str
    summary:      str
    detail:       str = ""


class ExtractionResult(BaseModel):
    """Container returned by the post-narration fact extraction call."""
    facts: list[ExtractedFact] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 – Narrative Generation Schema: Orchestrator → Gemini → Discord
# ─────────────────────────────────────────────────────────────────────────────

class MechanicalTruth(BaseModel):
    """
    The unalterable mechanical facts injected into the Gemini narrative prompt.
    Gemini must not contradict any field here.
    """
    action_type:        str
    difficulty:         int
    dice_notation:      str
    roll_result:        int
    outcome:            ActionOutcome
    stat_changes:       list[StatDelta]
    status_change:      CharacterStatus | None
    rulebook_citations: list[str]


class NarrativeRequestPayload(BaseModel):
    """
    Phase 4 input – contains the player intent, the mechanical truth, and
    the retrieved story memory context. Gemini must not contradict any
    established fact or any mechanical field.
    """
    prompt_id:          str  = Field(default_factory=lambda: str(uuid.uuid4()))
    intent_id:          str
    player_intent:      str  = Field(..., description="Verbatim player input")
    mechanical_truth:   MechanicalTruth
    character_context:  CharacterSnapshot
    campaign_system:    str  = Field(..., description="Active TTRPG system name")
    story_context:      list[StoryFact] = Field(
        default_factory=list,
        description="Established world facts retrieved from story memory; "
                    "Gemini must treat these as immutable canon.",
    )
    requested_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MultimediaCue(BaseModel):
    """An optional multimedia asset triggered by the narrative outcome."""
    cue_type:   MultimediaType
    asset_url:  str
    label:      str = ""


class NarrativeResponsePayload(BaseModel):
    """
    Phase 4 output – the final payload sent to Discord.
    Contains the narrative text and any triggered multimedia cues.
    """
    prompt_id:     str
    intent_id:     str
    narrative:     str  = Field(..., description="Full narrative text from Gemini")
    embed_title:   str  = Field(default="", description="Short Discord embed title")
    multimedia:    list[MultimediaCue] = Field(default_factory=list)
    generated_at:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# GM Director Schemas (Task 3 — Two-Tier Storyteller Architecture)
# ─────────────────────────────────────────────────────────────────────────────

class SubAgentTask(BaseModel):
    """
    A single delegation unit produced by the GM Director's planning pass.
    Dispatched concurrently to a local Ollama node tagged actor or scribe.
    """
    task_id:              str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_type:            str = Field(
        ...,
        description="npc_dialogue | environmental_description | combat_flavour | item_description",
    )
    entity_name:          str = Field(..., description="NPC name, location, weapon, or item name")
    entity_role:          str = Field(..., description="One-sentence description of the entity's nature")
    scene_context:        str = Field(..., description="2-3 sentence scene brief for the sub-agent")
    player_action_context: str = Field(..., description="What the player did that triggered this task")
    tone:                 str = Field(default="gritty", description="gritty | menacing | humorous | …")
    max_words:            int = Field(default=80, ge=20, le=300)


class GMPlanResult(BaseModel):
    """
    The structured output of the GM Director's planning pass.
    Produced internally — never shown to the player.
    """
    sub_tasks:       list[SubAgentTask] = Field(default_factory=list)
    direct_elements: list[str]          = Field(
        default_factory=list,
        description="Scene elements the GM will narrate directly without sub-agent delegation",
    )


class SubAgentResult(BaseModel):
    """
    The result returned by a single sub-agent execution.
    Aggregated by SubAgentDispatcher and handed to the GM for synthesis.
    """
    task:            SubAgentTask
    raw_output:      str             = Field(..., description="Uncensored raw text from the sub-agent node")
    node_name:       str             = Field(default="unknown", description="Which Ollama node handled this task")
    ttft_ms:         int | None      = Field(default=None, description="Time-to-first-token in ms for this task")
    brand_violation: bool            = Field(
        default=False,
        description="True if a brand violation was detected and stripped (best-effort fallback)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Top-Level Pipeline Result (persisted to action_log)
# ─────────────────────────────────────────────────────────────────────────────

class PipelineResult(BaseModel):
    """
    Aggregate of all four pipeline phases. Written to the action_log table
    for full session replay capability.
    """
    intent:      IntentPayload
    resolution:  OllamaResolutionPayload
    commit:      StateCommitPayload
    narrative:   NarrativeResponsePayload
    pipeline_duration_ms: int = 0

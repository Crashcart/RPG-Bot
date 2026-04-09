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
    Contains intent, full character state, retrieved rulebook context, and
    the Rolling Vault history block for long-session continuity.
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
    # Rolling Vault: formatted history string injected before the prompt.
    # Empty on the very first turn of a campaign.
    rolling_context:    str = Field(
        default="",
        description=(
            "Bounded session history from the Rolling Vault — summaries of older "
            "turns plus verbatim recent turns. Prepended to every Ollama prompt to "
            "prevent context-window overflow."
        ),
    )
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


# ─────────────────────────────────────────────────────────────────────────────
# Task 4 — Living Discord Immersion Layer Schemas
# ─────────────────────────────────────────────────────────────────────────────

class ThreadEvent(str, Enum):
    """
    Signals whether the Discord bot should manage an ephemeral combat thread.

    COMBAT  – A combat action occurred this turn.  The bot opens a new thread
              if none is open for this channel, or posts to the existing one.
    CLOSE   – The encounter has ended.  The bot posts a summary and archives
              the thread.
    """
    COMBAT = "combat"
    CLOSE  = "close"


class TTSCue(BaseModel):
    """
    A text-to-speech audio cue for Discord voice channel playback.

    Generated for every npc_dialogue sub-agent result.  The Discord bot
    uses edge-tts with the specified voice_id to generate audio and plays
    it sequentially in the voice channel after ambient audio starts.
    """
    entity_name: str  = Field(..., description="NPC name or source label")
    text:        str  = Field(..., description="Dialogue text to speak aloud")
    voice_id:    str  = Field(
        default="en-US-GuyNeural",
        description="edge-tts voice name — tied to the Ollama node that generated this dialogue",
    )
    node_name:   str  = Field(default="unknown", description="Originating sub-agent node")


class SFXCue(BaseModel):
    """
    A one-shot sound effect cue for Discord voice channel playback.
    Generated by the sound_director sub-agent or the GM Director.
    """
    sfx_key:  str   = Field(..., description="echo_vault key or ElevenLabs text description")
    volume:   float = Field(default=0.7, ge=0.0, le=1.0)
    delay_ms: int   = Field(default=0, ge=0, description="Milliseconds after narrative delivery")
    source:   str   = Field(
        default="vault",
        description="vault (pre-recorded file) | elevenlabs (AI-generated on demand)",
    )


class MusicCue(BaseModel):
    """
    A music cue for Discord voice channel background music.

    Primary path: Gemini Lyria 3 generates actual audio bytes from music_prompt.
    Fallback (when music_model='lavalink'): lavalink_query is used for YouTube/SoundCloud search.
    audio_url is populated post-generation by the orchestrator; empty = not yet generated.
    The VoiceManager loops the audio using FFmpeg '-stream_loop -1' for continuous ambient music.
    """
    scene_type:     str   = Field(..., description="combat | exploration | social | tension | rest | tavern")
    music_prompt:   str   = Field(
        ...,
        description=(
            "Descriptive prose for Lyria 3 — e.g. 'tense dungeon chase, "
            "staccato strings, low brass pulses, 140bpm'. Be specific about "
            "instrumentation, tempo, and mood."
        ),
    )
    lavalink_query: str   = Field(
        default="",
        description="Fallback search string for Lavalink YouTube/SoundCloud mode",
    )
    audio_url:      str   = Field(
        default="",
        description="Media-proxy URL to the generated .mp3; empty until orchestrator populates it",
    )
    volume:         float = Field(default=0.45, ge=0.0, le=1.0)
    crossfade_s:    float = Field(default=2.0, ge=0.0, le=10.0)


class ChannelDirective(BaseModel):
    """
    Instruction for the Discord bot to manipulate a player's channel access.

    Emitted when the narrative warrants a physical location change —
    the player is captured, escapes, is rescued, etc.

    action options:
      "move_to"  – grant read-only access to channel_key, restrict current channel
      "restore"  – restore full access to the main game channel
    """
    action:      str = Field(..., description="move_to | restore")
    channel_key: str = Field(
        ...,
        description="Semantic key for the target channel: dungeon | prison | hospital | main",
    )
    reason:      str = Field(default="", description="Short explanation for audit logs")


class NarrativeResponsePayload(BaseModel):
    """
    Phase 4 output – the final payload sent to Discord.

    Core fields (prompt_id, intent_id, narrative, embed_title) are unchanged.
    Task 4 fields carry the Living Discord immersion layer instructions.
    The Discord bot reads each field independently; all Task 4 fields are
    optional so older bot versions degrade gracefully.
    """
    prompt_id:     str
    intent_id:     str
    narrative:     str  = Field(..., description="Full narrative prose — posted to main channel")
    embed_title:   str  = Field(default="", description="Short Discord embed title")
    multimedia:    list[MultimediaCue] = Field(default_factory=list)

    # ── Task 4: Paranoia Whisper (Private Perception) ─────────────────────
    whisper: str | None = Field(
        default=None,
        description="Secret GM insight DMed to the player — what their skeptical eye notices "
                    "that no one else in the scene would catch. 2-3 sentences.",
    )

    # ── Task 4: Ghost Sheet / Ephemeral Thread ────────────────────────────
    thread_event:   ThreadEvent | None = Field(
        default=None,
        description="If set, the bot manages a combat/encounter thread on this message.",
    )
    thread_title:   str = Field(
        default="Encounter Details",
        description="Thread name, e.g. 'Combat – Thug Alley'",
    )
    thread_content: str | None = Field(
        default=None,
        description="Mechanical grit posted inside the thread: dice rolls, damage, "
                    "inventory changes, rulebook citations. Never shown in main channel.",
    )

    # ── Task 4: Voice Channel Puppeteering ────────────────────────────────
    ambient_audio_key: str | None = Field(
        default=None,
        description="Key for the ambient audio file to loop in the voice channel "
                    "(e.g. 'tavern_chatter', 'dungeon_ambience', 'combat_tension').",
    )
    tts_cues: list[TTSCue] = Field(
        default_factory=list,
        description="Ordered list of NPC dialogue cues to speak aloud via TTS, "
                    "each with its Ollama node's unique voice profile.",
    )

    # ── Task 4: Channel Manipulation ─────────────────────────────────────
    channel_directive: ChannelDirective | None = Field(
        default=None,
        description="If set, the bot moves the player's Discord channel access — "
                    "into the dungeon, prison, hospital, or back to main.",
    )

    # ── Driftnet: World-bound broadcast channel ───────────────────────────
    driftnet_channel_id: str = Field(
        default="",
        description=(
            "Discord channel snowflake for this world's driftnet channel. "
            "When set, the bot mirrors the narrative embed there in addition "
            "to the player's own channel."
        ),
    )

    # ── Multimedia: Music, SFX, Images, Handouts ─────────────────────────
    sfx_cues: list[SFXCue] = Field(
        default_factory=list,
        description="Ordered list of one-shot SFX to play after the narrative lands.",
    )
    music_cue: MusicCue | None = Field(
        default=None,
        description=(
            "Background music cue. The orchestrator populates audio_url after Lyria "
            "generation; the bot loops the track until a new cue arrives."
        ),
    )
    scene_image_prompt: str | None = Field(
        default=None,
        description=(
            "Stable Diffusion / ComfyUI prompt for scene art. The bot triggers "
            "ImageGenService and edits the narrative embed with the resulting image."
        ),
    )
    npc_portrait_name: str | None = Field(
        default=None,
        description="NPC name to generate a portrait for on first encounter.",
    )
    handout_id: str | None = Field(
        default=None,
        description="UUID of a handout to deliver automatically to the player after this turn.",
    )

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    trigger_scene_image:  bool      = Field(
        default=False,
        description="True when this is a major scene transition that warrants new scene art.",
    )
    trigger_npc_portrait: str | None = Field(
        default=None,
        description="NPC name if this is the player's first encounter with this NPC.",
    )


class SubAgentResult(BaseModel):
    """
    The result returned by a single sub-agent execution.
    Aggregated by SubAgentDispatcher and handed to the GM for synthesis.
    """
    task:            SubAgentTask
    raw_output:      str             = Field(..., description="Uncensored raw text from the sub-agent node")
    node_name:       str             = Field(default="unknown", description="Which Ollama node handled this task")
    voice_id:        str             = Field(
        default="en-US-GuyNeural",
        description="edge-tts voice name for this node — carried into TTSCue for voice channel playback",
    )
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


# ─────────────────────────────────────────────────────────────────────────────
# Async Session Feature Schemas
# ─────────────────────────────────────────────────────────────────────────────

# ── Chronicle Recap ───────────────────────────────────────────────────────────

class RecapRequest(BaseModel):
    """
    Request a 'Previously on…' catch-up summary for a player who was offline.
    The orchestrator queries everything in action_log and story_context that
    occurred after the player's last message, then asks Gemini to produce a
    concise bulleted summary.
    """
    player_id:   str = Field(..., description="Discord snowflake of the requesting player")
    guild_id:    str = Field(..., description="Discord server snowflake")
    campaign_id: str = Field(..., description="Campaign UUID")


class RecapResponse(BaseModel):
    """Ephemeral 'Previously on…' summary delivered to the requesting player."""
    player_id:        str
    campaign_id:      str
    recap_text:       str   = Field(..., description="Bulleted narrative summary")
    events_covered:   int   = Field(default=0, description="Number of action_log rows summarised")
    since_timestamp:  datetime | None = Field(
        default=None,
        description="The timestamp of the player's last action (recap covers everything after this)",
    )
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Campfire Mode ─────────────────────────────────────────────────────────────

class PresenceUpdate(BaseModel):
    """Posted by the Discord bot whenever a player's online status changes."""
    player_id:  str  = Field(..., description="Discord snowflake")
    guild_id:   str  = Field(..., description="Discord server snowflake")
    online:     bool = Field(..., description="True = came online, False = went offline")


class CampfireStatus(BaseModel):
    """
    Current Campfire Mode state for a guild.
    When active, the pipeline allows only 'downtime RP' actions and refuses
    to advance the main narrative past the current scene.
    """
    guild_id:        str
    active:          bool        = False
    absent_players:  list[str]   = Field(
        default_factory=list,
        description="Discord snowflakes of offline players who triggered campfire mode",
    )
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Async Downtime Tasks ──────────────────────────────────────────────────────

class DowntimeSubmitRequest(BaseModel):
    """
    Submitted by a player via /downtime before they log off.
    The GM resolves the task in the background while the player sleeps.
    """
    player_id:      str = Field(..., description="Discord snowflake")
    guild_id:       str = Field(..., description="Discord server snowflake")
    campaign_id:    str = Field(..., description="Campaign UUID")
    description:    str = Field(
        ...,
        description="What the character does during downtime, in the player's own words",
        max_length=1000,
    )
    duration_hours: int = Field(
        default=8,
        ge=1,
        le=168,
        description="Real-world hours before the task resolves (default 8 = overnight)",
    )


class DowntimeTaskStatus(BaseModel):
    """Current state of a single downtime task."""
    task_id:          str
    description:      str
    status:           str   = Field(..., description="pending | resolving | complete | failed")
    duration_hours:   int
    submitted_at:     datetime
    resolves_at:      datetime
    resolved_at:      datetime | None = None
    result_narrative: str | None      = None
    notified:         bool            = False


class DowntimePendingNotification(BaseModel):
    """
    Returned by /api/downtime/notifications — the Discord bot polls this
    endpoint, DMs the player, then marks the notification delivered.
    """
    task_id:          str
    player_id:        str
    result_narrative: str
    character_name:   str = ""


# ── Retcon ────────────────────────────────────────────────────────────────────

class RetconRequest(BaseModel):
    """
    Admin request to roll back a specific action and restore pre-action state.
    The action_log row is flagged retconned=TRUE (never deleted) for audit purposes.
    """
    intent_id:    str = Field(..., description="UUID of the action_log entry to retcon")
    admin_id:     str = Field(..., description="Discord snowflake of the admin issuing the retcon")
    reason:       str = Field(default="", description="Short explanation for the audit log")


class RetconResponse(BaseModel):
    """Confirmation that a retcon was applied successfully."""
    intent_id:       str
    character_id:    str
    restored_stats:  dict[str, Any]
    retconned_at:    datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Admin Backchannel Schemas
# ─────────────────────────────────────────────────────────────────────────────

class DirectiveType(str, Enum):
    SCENE_DIRECTIVE = "scene_directive"   # trigger env event next scene
    NPC_HINT        = "npc_hint"          # have NPC drop a specific hint
    WORLD_EVENT     = "world_event"       # something happens in the world right now
    PACING_NOTE     = "pacing_note"       # "make this moment feel climactic"
    CORRECTION      = "correction"        # subtle fix without railroading


class GMDirectiveRequest(BaseModel):
    """
    An OOC (Out-of-Character) admin command sent through the White Portal
    Backchannel to the GM Engine.  The GM weaves it into the next player
    action's narrative as a high-priority world-management event.

    This is the ONLY channel through which an admin can influence the story
    mechanically.  Admin accounts in Discord are treated as standard players
    (Fair Play Sandbox).
    """
    campaign_id:     str           = Field(..., description="Campaign UUID")
    admin_id:        str           = Field(..., description="Admin Discord snowflake")
    directive_type:  DirectiveType = Field(default=DirectiveType.SCENE_DIRECTIVE)
    directive_text:  str           = Field(
        ...,
        description="Plain-English instruction to the GM Engine",
        max_length=800,
    )
    priority:        int           = Field(
        default=5,
        ge=1,
        le=10,
        description="Injection urgency: 10 = inject unconditionally, 1 = only if scene context fits",
    )


class GMDirective(BaseModel):
    """A single GM directive record, as stored in the database."""
    directive_id:    str
    campaign_id:     str
    admin_id:        str
    directive_type:  DirectiveType
    directive_text:  str
    priority:        int
    status:          str  = "pending"    # pending | consumed | cancelled
    submitted_at:    datetime
    consumed_at:     datetime | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Agent Vector-Space Communication — NPC/GM Sync (TDR §2)
# ─────────────────────────────────────────────────────────────────────────────

class EmotionIntentCode(int, Enum):
    """
    Lightweight integer dictionary of common RPG emotional / intent states.

    These 4-byte IDs are broadcast on the AgentSyncBus so NPC models can
    shift their behaviour without requiring full text tokenisation.
    Ranges: 1–19 combat, 20–39 social, 40–59 environmental, 60–79 player,
            80–99 world events.
    """
    # ── Combat states ─────────────────────────────────────────────────────
    NEUTRAL        = 0
    AGGRO          = 1    # NPC has entered combat mode
    FLEE           = 2    # NPC is trying to escape
    DEFEND         = 3    # NPC is holding a defensive posture
    ALLY_DOWN      = 4    # An allied NPC has been defeated
    STUNNED        = 5    # NPC cannot act this turn
    ENRAGED        = 6    # NPC has entered berserk mode (attack penalty / damage bonus)
    SURRENDERING   = 7    # NPC has dropped their weapon

    # ── Social / emotional states ─────────────────────────────────────────
    CURIOUS        = 20   # NPC is interested in the player
    SUSPICIOUS     = 21   # NPC senses something is wrong
    TRUSTING       = 22   # NPC believes the player's story
    DECEIVED       = 23   # NPC has been successfully bluffed
    HOSTILE        = 24   # NPC is verbally antagonistic
    INTIMIDATED    = 25   # NPC is cowering or compliant from fear
    GRIEVING       = 26   # NPC has suffered a recent loss
    ELATED         = 27   # NPC is joyful / celebrating

    # ── Environmental awareness ───────────────────────────────────────────
    DARKNESS       = 40   # Lights out / very low visibility
    ON_FIRE        = 41   # Area is burning; NPC must account for smoke/heat
    HAZARD_PRESENT = 42   # Generic environmental hazard nearby
    SECURE         = 43   # NPC feels safe in current position

    # ── Player-action reactions ───────────────────────────────────────────
    WEAPON_DRAWN   = 60   # Player has drawn a weapon this turn
    SPELL_CAST     = 61   # Player cast a spell this turn
    ITEM_USED      = 62   # Player consumed or deployed an item
    PERSUADE_ATTEMPT = 63 # Player attempted a social action

    # ── World-scale events ────────────────────────────────────────────────
    ALARM_RAISED   = 80   # Alarm / reinforcements incoming
    OBJECTIVE_MET  = 81   # A shared objective was achieved this turn
    PLOT_REVEALED  = 82   # A major plot secret became known


class EmotionHashPayload(BaseModel):
    """
    A compact emotion/intent state for a single NPC agent or the GM.

    The AgentSyncBus attaches one of these to every broadcast so receiving
    NPC models can update their behaviour without parsing prose.
    """
    code:        EmotionIntentCode = Field(
        ...,
        description="Integer emotion/intent code from EmotionIntentCode",
    )
    intensity:   int = Field(
        default=5,
        ge=1,
        le=10,
        description="Intensity of the emotional state 1 (barely present) – 10 (overwhelming)",
    )
    target_id:   str | None = Field(
        default=None,
        description="Optional entity ID (NPC or player) that triggered this state",
    )


class EpistemicBoundary(BaseModel):
    """
    Knowledge segregation envelope for a single NPC agent (TDR §3 — Epistemic Boundaries).

    Defines what fragments of the SceneStateVector this NPC is allowed to receive.
    An NPC's knowledge is strictly limited to its immediate sensory radius;
    it must not receive hidden GM information (cursed items, upcoming traps, etc.).
    """
    npc_id:          str  = Field(..., description="Unique NPC identifier (name slug or UUID)")
    npc_name:        str  = Field(..., description="Display name used in narrative")
    sensory_radius:  int  = Field(
        default=30,
        ge=0,
        le=300,
        description="In-world perception radius in feet; determines info cutoff",
    )
    knows_player_hp: bool = Field(
        default=False,
        description="True only for medic/healer archetypes that can assess injuries",
    )
    knows_curses:    bool = Field(
        default=False,
        description="True only if NPC has mystical sight (detect magic, etc.)",
    )
    allowed_codes:   list[EmotionIntentCode] = Field(
        default_factory=list,
        description=(
            "Subset of EmotionIntentCodes this NPC is allowed to receive. "
            "Empty list = receive all non-secret codes."
        ),
    )
    fog_of_war:      dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary per-NPC knowledge exclusions keyed by scene element name",
    )


class SceneStateVector(BaseModel):
    """
    Compressed semantic representation of the current scene state (TDR §2-A/B).

    Produced by AgentSyncBus.compress() after every Phase 2 adjudication.
    The full vector is held by the GM layer; individual NPC agents receive
    a filtered projection via apply_epistemic_boundary().

    This is NOT a mathematical float vector — it is a structured key-value
    envelope designed for low-latency serialisation and direct context
    injection into Ollama NPC prompts (TDR §2-B Step 3).
    """
    vector_id:       str  = Field(default_factory=lambda: str(uuid.uuid4()))
    campaign_id:     str  = Field(..., description="Campaign UUID this vector belongs to")
    intent_id:       str  = Field(..., description="Intent that triggered this state update")

    # ── Mechanical snapshot ───────────────────────────────────────────────
    action_type:     str  = Field(..., description="What the player just did")
    outcome:         ActionOutcome
    roll_result:     int
    difficulty:      int

    # ── Emotion/intent hashes — the compressed broadcast payload ──────────
    gm_emotion:      EmotionHashPayload = Field(
        ...,
        description="The GM's internal assessment of the scene mood",
    )
    npc_emotions:    list[tuple[str, EmotionHashPayload]] = Field(
        default_factory=list,
        description="Per-NPC emotion states: [(npc_id, EmotionHashPayload), ...]",
    )

    # ── Scene metadata ────────────────────────────────────────────────────
    active_npcs:     list[str] = Field(
        default_factory=list,
        description="NPC IDs present in the current scene",
    )
    environment:     str  = Field(
        default="unknown",
        description="Current environment type (dungeon, tavern, wilderness, etc.)",
    )
    vibe_key:        str  = Field(
        default="neutral",
        description="Current VibeStream atmosphere key (tense, spooky, chaotic, etc.)",
    )

    # ── Hidden GM secrets (never forwarded to NPC agents) ─────────────────
    gm_secrets:      dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Hidden GM information (traps, curses, NPC motivations, upcoming twists). "
            "NEVER included in NPCSyncContext payloads broadcast to NPC agents."
        ),
    )

    compressed_at:   datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NPCSyncContext(BaseModel):
    """
    The filtered scene state injected directly into an NPC Ollama prompt (TDR §2-B Step 3).

    Produced by AgentSyncBus.apply_epistemic_boundary() — it is the
    SceneStateVector with all information outside the NPC's EpistemicBoundary
    stripped away, ready for direct context injection.
    """
    npc_id:          str
    npc_name:        str
    vector_id:       str  = Field(..., description="Parent SceneStateVector ID")
    campaign_id:     str

    # ── What this NPC knows ───────────────────────────────────────────────
    perceived_action: str  = Field(
        ...,
        description="What the NPC sensed (may differ from actual action if out of radius)",
    )
    perceived_outcome: str = Field(
        default="",
        description="What the NPC observed of the outcome",
    )
    emotion_state:     EmotionHashPayload = Field(
        ...,
        description="This NPC's current emotion/intent hash after state update",
    )
    visible_emotions:  list[tuple[str, EmotionHashPayload]] = Field(
        default_factory=list,
        description="Emotion hashes of other NPCs this NPC can perceive",
    )
    environment:       str = Field(default="unknown")
    vibe_key:          str = Field(default="neutral")

    injected_at:       datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VibeStream(BaseModel):
    """
    Low-dimensional background atmosphere stream (TDR §3 Option 3).

    Maintained in Redis per campaign so every NPC naturally adapts its
    dialogue generation to match the room's tone without explicit GM
    instructions on every line.
    """
    campaign_id: str
    vibe_key:    str = Field(
        ...,
        description=(
            "Current atmospheric label: neutral | tense | spooky | chaotic | "
            "serene | ominous | celebratory | mournful | combat"
        ),
    )
    intensity:   int = Field(
        default=5,
        ge=1,
        le=10,
        description="Atmosphere intensity 1 (subtle) – 10 (overwhelming)",
    )
    source:      str = Field(
        default="auto",
        description="What triggered the vibe update: auto | gm_override | player_action",
    )
    updated_at:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HiveMindCombatRequest(BaseModel):
    """
    Triggers simultaneous parallel NPC combat turn resolution (TDR §3 Option 2).

    The GM broadcasts the board state to all enemy NPC agents at once;
    they calculate their moves in parallel and the GM resolves conflicts.
    Bypasses the sequential one-NPC-at-a-time bottleneck.
    """
    campaign_id:  str  = Field(..., description="Campaign UUID")
    vector_id:    str  = Field(..., description="SceneStateVector ID representing the board state")
    npc_ids:      list[str] = Field(
        ...,
        min_length=1,
        description="NPC IDs to activate simultaneously",
    )
    round_number: int  = Field(default=1, ge=1, description="Combat round number")
    time_limit_ms: int = Field(
        default=3000,
        ge=500,
        le=30000,
        description="Hard deadline for NPC deliberation before the GM resolves with partial data",
    )


class HiveMindCombatResult(BaseModel):
    """
    Aggregate result of a parallel hive-mind combat resolution pass.

    Each NPC's chosen action is returned alongside any GM conflict resolutions
    (e.g. two NPCs targeted the same player; the GM adjusts one).
    """
    vector_id:    str
    round_number: int
    npc_actions:  list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "List of {npc_id, action, target, emotion_state} dicts — "
            "one per NPC that responded within the time limit"
        ),
    )
    conflicts_resolved: int = Field(
        default=0,
        description="Number of targeting conflicts resolved by the GM layer",
    )
    timed_out_npcs: list[str] = Field(
        default_factory=list,
        description="NPC IDs that did not respond within time_limit_ms; GM uses default action",
    )
    resolved_at:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

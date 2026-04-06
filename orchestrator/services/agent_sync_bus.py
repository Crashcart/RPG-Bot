"""
Agent Sync Bus — Multi-Agent Vector-Space Communication (TDR §2)
================================================================
Central high-speed message bus connecting the AI Game Master with
lightweight NPC agents.

Instead of passing full text prompts between the GM and NPCs after every
player action, the bus compresses the current scene state into a
SceneStateVector and selectively broadcasts a filtered NPCSyncContext to
each active NPC agent.  Each NPC receives only the information it is
epistemically allowed to see (TDR §3 — Epistemic Boundaries), preventing
meta-gaming and maintaining fog-of-war.

Three optional performance modes (TDR §3 Options):
  Option 1 — Direct context injection (always active)
      NPCSyncContext payloads are injected directly into the Ollama sub-agent
      prompt builder, bypassing repeat scene tokenisation.

  Option 2 — Hive-Mind Combat (resolve_hive_mind_combat)
      During combat, all enemy NPCs are activated simultaneously.  Each
      calculates its move in parallel; the GM resolves conflicts.

  Option 3 — Vibe Stream (update_vibe / get_vibe)
      A low-dimensional atmosphere key is persisted in Redis per campaign.
      NPC dialogue prompts are automatically prefixed with the current vibe
      so they adapt their tone without explicit GM instructions per turn.

Security
--------
  • The bus is an internal asyncio service — it is never exposed directly
    over HTTP.  The /api/npc/sync endpoint validates the campaign before
    calling broadcast().
  • gm_secrets are stripped before any NPCSyncContext is produced.
  • Emotion code filtering via EpistemicBoundary.allowed_codes prevents
    higher-level strategic information leaking to rank-and-file NPCs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.services.cache import CacheService
    from orchestrator.services.node_router import NodeRouter

from orchestrator.schemas.payloads import (
    ActionOutcome,
    EmotionHashPayload,
    EmotionIntentCode,
    EpistemicBoundary,
    HiveMindCombatRequest,
    HiveMindCombatResult,
    NPCSyncContext,
    OllamaResolutionPayload,
    SceneStateVector,
    VibeStream,
)

logger = logging.getLogger(__name__)

# Redis key templates
_VIBE_KEY       = "ironclad:vibe:{campaign_id}"
_VECTOR_KEY     = "ironclad:scene_vector:{campaign_id}"
_VECTOR_TTL     = 300   # seconds — vectors are ephemeral; recalculated each turn

# Vibe inference: outcome × action_type → vibe_key
_OUTCOME_VIBE_MAP: dict[str, str] = {
    "critical_success": "celebratory",
    "success":          "neutral",
    "partial_success":  "tense",
    "failure":          "ominous",
    "critical_failure": "chaotic",
}

_COMBAT_ACTION_PREFIXES = ("melee", "ranged", "spell_attack", "combat", "attack")


def _infer_vibe(outcome: ActionOutcome, action_type: str) -> str:
    """Derive a vibe key from the mechanical outcome and action type."""
    if any(action_type.startswith(p) for p in _COMBAT_ACTION_PREFIXES):
        if outcome in (ActionOutcome.CRITICAL_SUCCESS, ActionOutcome.SUCCESS):
            return "combat"
        return "tense"
    return _OUTCOME_VIBE_MAP.get(outcome.value, "neutral")


def _infer_gm_emotion(outcome: ActionOutcome, action_type: str) -> EmotionHashPayload:
    """Build a GM emotion hash from the turn outcome."""
    is_combat = any(action_type.startswith(p) for p in _COMBAT_ACTION_PREFIXES)
    if is_combat:
        code = EmotionIntentCode.AGGRO
        intensity = {
            ActionOutcome.CRITICAL_SUCCESS: 8,
            ActionOutcome.SUCCESS:          6,
            ActionOutcome.PARTIAL_SUCCESS:  5,
            ActionOutcome.FAILURE:          4,
            ActionOutcome.CRITICAL_FAILURE: 3,
        }.get(outcome, 5)
    else:
        code = {
            ActionOutcome.CRITICAL_SUCCESS: EmotionIntentCode.ELATED,
            ActionOutcome.SUCCESS:          EmotionIntentCode.TRUSTING,
            ActionOutcome.PARTIAL_SUCCESS:  EmotionIntentCode.SUSPICIOUS,
            ActionOutcome.FAILURE:          EmotionIntentCode.HOSTILE,
            ActionOutcome.CRITICAL_FAILURE: EmotionIntentCode.ENRAGED,
        }.get(outcome, EmotionIntentCode.NEUTRAL)
        intensity = 5

    return EmotionHashPayload(code=code, intensity=intensity)


def _default_npc_emotion(
    outcome: ActionOutcome,
    action_type: str,
    npc_id: str,
) -> EmotionHashPayload:
    """
    Derive a plausible starting emotion for an NPC that has no pre-existing state.
    In a full system this would query each NPC's personality profile from DB.
    """
    is_combat = any(action_type.startswith(p) for p in _COMBAT_ACTION_PREFIXES)
    if is_combat:
        code = EmotionIntentCode.AGGRO if outcome in (
            ActionOutcome.CRITICAL_SUCCESS,
            ActionOutcome.SUCCESS,
        ) else EmotionIntentCode.FLEE
    else:
        code = EmotionIntentCode.SUSPICIOUS if outcome == ActionOutcome.FAILURE else EmotionIntentCode.NEUTRAL
    return EmotionHashPayload(code=code, intensity=5, target_id=npc_id)


class AgentSyncBus:
    """
    Central message bus for GM ↔ NPC agent communication.

    Wired as a long-lived service in main.py.  The GMDirector calls
    broadcast() after the planning pass so all NPC sub-agents receive a
    fresh NPCSyncContext before they generate their dialogue.
    """

    def __init__(
        self,
        node_router: "NodeRouter",
        cache:       "CacheService | None" = None,
    ) -> None:
        self._router = node_router
        self._cache  = cache

    # ── Public API ────────────────────────────────────────────────────────────

    def compress(
        self,
        resolution:  OllamaResolutionPayload,
        campaign_id: str,
        active_npcs: list[str] | None = None,
        environment: str = "unknown",
        gm_secrets:  dict[str, Any] | None = None,
    ) -> SceneStateVector:
        """
        Compress Phase-2 adjudication output into a compact SceneStateVector.

        The GM layer retains the full vector (including gm_secrets).
        NPC agents receive a filtered projection via apply_epistemic_boundary().
        """
        vibe_key   = _infer_vibe(resolution.outcome, resolution.action_type)
        gm_emotion = _infer_gm_emotion(resolution.outcome, resolution.action_type)

        npc_ids = active_npcs or []
        npc_emotions: list[tuple[str, EmotionHashPayload]] = [
            (nid, _default_npc_emotion(resolution.outcome, resolution.action_type, nid))
            for nid in npc_ids
        ]

        return SceneStateVector(
            campaign_id  = campaign_id,
            intent_id    = resolution.intent_id,
            action_type  = resolution.action_type,
            outcome      = resolution.outcome,
            roll_result  = resolution.roll_result,
            difficulty   = resolution.difficulty,
            gm_emotion   = gm_emotion,
            npc_emotions = npc_emotions,
            active_npcs  = npc_ids,
            environment  = environment,
            vibe_key     = vibe_key,
            gm_secrets   = gm_secrets or {},
        )

    def apply_epistemic_boundary(
        self,
        vector:   SceneStateVector,
        boundary: EpistemicBoundary,
    ) -> NPCSyncContext:
        """
        Apply knowledge segregation to produce an NPC-safe context payload.

        Strips gm_secrets unconditionally.  Filters npc_emotions and
        perceived_action to only what falls within the NPC's sensory radius
        and allowed_codes list (TDR §3 — Epistemic Boundaries).
        """
        allowed_codes_set = set(boundary.allowed_codes)

        # Filter peer NPC emotion states: only include codes the NPC is allowed to see
        visible_emotions: list[tuple[str, EmotionHashPayload]] = []
        for (peer_id, emotion) in vector.npc_emotions:
            if peer_id == boundary.npc_id:
                continue  # exclude self
            if allowed_codes_set and emotion.code not in allowed_codes_set:
                continue  # not in this NPC's allowed perception set
            visible_emotions.append((peer_id, emotion))

        # Find this NPC's own emotion state
        own_emotion = next(
            (e for (nid, e) in vector.npc_emotions if nid == boundary.npc_id),
            EmotionHashPayload(code=EmotionIntentCode.NEUTRAL, intensity=5),
        )

        # Fog-of-war: if NPC is outside sensory radius, describe only noise
        perceived_action = vector.action_type
        perceived_outcome = vector.outcome.value
        if boundary.fog_of_war.get(vector.action_type):
            perceived_action  = "distant commotion"
            perceived_outcome = "unclear"

        return NPCSyncContext(
            npc_id            = boundary.npc_id,
            npc_name          = boundary.npc_name,
            vector_id         = vector.vector_id,
            campaign_id       = vector.campaign_id,
            perceived_action  = perceived_action,
            perceived_outcome = perceived_outcome,
            emotion_state     = own_emotion,
            visible_emotions  = visible_emotions,
            environment       = vector.environment,
            vibe_key          = vector.vibe_key,
        )

    async def broadcast(
        self,
        vector:     SceneStateVector,
        boundaries: list[EpistemicBoundary],
    ) -> list[NPCSyncContext]:
        """
        Simultaneously produce filtered NPCSyncContext payloads for all active NPC agents.

        Results are returned in the same order as *boundaries*.  This is a
        pure in-process fan-out — there is no network hop; the contexts are
        later injected into Ollama sub-agent prompts by SubAgentDispatcher.
        """
        if not boundaries:
            return []

        contexts = [
            self.apply_epistemic_boundary(vector, boundary)
            for boundary in boundaries
        ]

        if self._cache:
            asyncio.create_task(self._persist_vector(vector))

        logger.info(
            "AgentSyncBus.broadcast: vector=%s campaign=%s npcs=%d",
            vector.vector_id,
            vector.campaign_id,
            len(contexts),
        )
        return contexts

    async def resolve_hive_mind_combat(
        self,
        request: HiveMindCombatRequest,
        boundaries: list[EpistemicBoundary],
        vector: SceneStateVector,
    ) -> HiveMindCombatResult:
        """
        Parallel NPC combat resolution (TDR §3 Option 2 — Hive-Mind Combat).

        All NPC agents receive the board state simultaneously and compute
        their optimal moves concurrently.  The GM resolves targeting conflicts.
        NPCs that miss the deadline are assigned a default action by the GM.
        """
        contexts = await self.broadcast(vector, boundaries)
        deadline = request.time_limit_ms / 1000.0

        async def _resolve_one(ctx: NPCSyncContext) -> dict[str, Any]:
            """Ask an Ollama node for a single NPC's combat action."""
            t_start = time.monotonic()
            try:
                client = await self._router.get_ollama_client_for_role("actor")
                if client is None:
                    client = await self._router.get_ollama_client()

                prompt = _build_combat_action_prompt(ctx, request.round_number)
                raw = await asyncio.wait_for(
                    client.generate(
                        system_prompt=_HIVE_MIND_SYSTEM_PROMPT,
                        user_prompt=prompt,
                        max_tokens=80,
                    ),
                    timeout=deadline,
                )
                elapsed_ms = int((time.monotonic() - t_start) * 1000)
                return {
                    "npc_id":       ctx.npc_id,
                    "action":       raw.strip(),
                    "target":       None,
                    "emotion_state": ctx.emotion_state.model_dump(),
                    "elapsed_ms":   elapsed_ms,
                    "timed_out":    False,
                }
            except asyncio.TimeoutError:
                return {
                    "npc_id":       ctx.npc_id,
                    "action":       "holds position",
                    "target":       None,
                    "emotion_state": ctx.emotion_state.model_dump(),
                    "elapsed_ms":   request.time_limit_ms,
                    "timed_out":    True,
                }
            except Exception as exc:
                logger.warning(
                    "HiveMindCombat: NPC %s resolution error: %s",
                    ctx.npc_id, exc,
                )
                return {
                    "npc_id":       ctx.npc_id,
                    "action":       "holds position",
                    "target":       None,
                    "emotion_state": ctx.emotion_state.model_dump(),
                    "elapsed_ms":   0,
                    "timed_out":    True,
                }

        raw_results = await asyncio.gather(
            *[_resolve_one(ctx) for ctx in contexts],
            return_exceptions=False,
        )

        timed_out = [r["npc_id"] for r in raw_results if r["timed_out"]]
        npc_actions = [r for r in raw_results if not r["timed_out"]]

        # Resolve simple targeting conflicts: if multiple NPCs picked the same
        # target, shift duplicates to adjacent targets (placeholder logic —
        # a full implementation queries the combat grid).
        conflicts = _resolve_targeting_conflicts(npc_actions)

        logger.info(
            "HiveMindCombat round=%d vector=%s npcs_resolved=%d timeouts=%d conflicts=%d",
            request.round_number,
            request.vector_id,
            len(npc_actions),
            len(timed_out),
            conflicts,
        )

        return HiveMindCombatResult(
            vector_id           = request.vector_id,
            round_number        = request.round_number,
            npc_actions         = npc_actions,
            conflicts_resolved  = conflicts,
            timed_out_npcs      = timed_out,
        )

    async def update_vibe(
        self,
        campaign_id: str,
        vibe_key:    str,
        intensity:   int = 5,
        source:      str = "auto",
    ) -> VibeStream:
        """
        Update the campaign's background atmosphere stream in Redis (TDR §3 Option 3).
        """
        vibe = VibeStream(
            campaign_id = campaign_id,
            vibe_key    = vibe_key,
            intensity   = intensity,
            source      = source,
        )
        if self._cache:
            try:
                key = _VIBE_KEY.format(campaign_id=campaign_id)
                await self._cache.set(key, vibe.model_dump_json())
            except Exception as exc:
                logger.warning("AgentSyncBus: failed to persist vibe: %s", exc)
        return vibe

    async def get_vibe(self, campaign_id: str) -> VibeStream | None:
        """Return the current VibeStream for a campaign from Redis, or None."""
        if not self._cache:
            return None
        try:
            key  = _VIBE_KEY.format(campaign_id=campaign_id)
            raw  = await self._cache.get(key)
            if raw:
                return VibeStream.model_validate_json(raw)
        except Exception as exc:
            logger.warning("AgentSyncBus: failed to read vibe from cache: %s", exc)
        return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _persist_vector(self, vector: SceneStateVector) -> None:
        """Cache the scene vector in Redis with a short TTL."""
        try:
            key = _VECTOR_KEY.format(campaign_id=vector.campaign_id)
            # Exclude gm_secrets from the cached copy for safety
            safe = vector.model_dump(exclude={"gm_secrets"})
            await self._cache.set(key, json.dumps(safe), ex=_VECTOR_TTL)
        except Exception as exc:
            logger.warning("AgentSyncBus: failed to persist vector: %s", exc)


# ── Module-level helpers ──────────────────────────────────────────────────────

_HIVE_MIND_SYSTEM_PROMPT = (
    "You are an NPC in a tactical RPG combat encounter. "
    "Respond with ONE short sentence describing your combat action (max 15 words). "
    "Be decisive. No prose, no explanation."
)


def _build_combat_action_prompt(ctx: NPCSyncContext, round_number: int) -> str:
    """Build a tightly scoped combat action prompt for a single NPC."""
    emotion_label = ctx.emotion_state.code.name.replace("_", " ").title()
    peers = ", ".join(nid for (nid, _) in ctx.visible_emotions) or "none nearby"
    return (
        f"[Round {round_number}] You are {ctx.npc_name} ({ctx.npc_id}).\n"
        f"Current mood: {emotion_label} (intensity {ctx.emotion_state.intensity}/10).\n"
        f"Environment: {ctx.environment} — vibe: {ctx.vibe_key}.\n"
        f"You perceived: {ctx.perceived_action} → {ctx.perceived_outcome}.\n"
        f"Allies in range: {peers}.\n"
        f"What is your next combat action?"
    )


def _resolve_targeting_conflicts(actions: list[dict[str, Any]]) -> int:
    """
    Detect and resolve duplicate target assignments.

    Returns the number of conflicts resolved.  In a full implementation this
    would consult the combat grid and redistribute NPCs to valid targets.
    Here we detect duplicates and mark them as unassigned so the GM Director
    can assign them during synthesis.
    """
    seen_targets: dict[str, str] = {}  # target → first npc_id that claimed it
    conflicts = 0
    for action in actions:
        target = action.get("target")
        if target is None:
            continue
        if target in seen_targets:
            action["target"] = None   # clear duplicate — GM will reassign
            conflicts += 1
        else:
            seen_targets[target] = action["npc_id"]
    return conflicts

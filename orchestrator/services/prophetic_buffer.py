"""
Prophetic Buffer — Predictive Asset Pre-Generation
===================================================
A background worker that fires after every completed pipeline turn and
speculatively pre-generates audio/text assets for the most likely next
player action — eliminating perceived latency on common follow-up beats.

How it works
------------
1. After each `PipelineResult` is written to action_log, the orchestrator
   calls `PropheticBuffer.enqueue(result)` (fire-and-forget).
2. The worker analyses the outcome (action_type, outcome, NPC list) and
   uses a heuristic to classify the likely next action category.
3. For each predicted category, it fires lightweight pre-generation tasks
   concurrently:
     • Text snippet (via the cloud storyteller) cached in Redis
     • Ambient audio key pre-selected and cached
4. When the real next turn arrives and matches a prefetched key, the pipeline
   reads from cache instead of regenerating — saving 1–3 seconds per turn.

Cache keys
----------
    ironclad:prophet:{intent_id}:text   → pre-generated narrative snippet
    ironclad:prophet:{intent_id}:audio  → predicted ambient_audio_key string
TTL: 120 seconds (covers a typical 60–90 s player deliberation window).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.services.cache       import CacheService
    from orchestrator.services.gemini_client import GeminiClient
    from orchestrator.services.claude_client import ClaudeClient
    from orchestrator.schemas.payloads     import PipelineResult

logger = logging.getLogger(__name__)

_PREFETCH_TTL     = 120   # seconds
_PREFETCH_TIMEOUT = 20    # seconds per prefetch task
_MAX_QUEUE        = 64    # drop oldest if queue backs up

# Heuristic: map outcome → likely follow-up action categories
_FOLLOW_UP_MAP: dict[str, list[str]] = {
    "critical_success": ["press_advantage", "social_interaction", "loot_search"],
    "success":          ["move_to_next_area", "social_interaction", "inventory_check"],
    "partial_success":  ["recover", "retry_skill", "assess_situation"],
    "failure":          ["escape_attempt", "defensive_action", "regroup"],
    "critical_failure": ["emergency_response", "flee", "call_for_help"],
}

_AMBIENT_PREDICTION: dict[str, str] = {
    "press_advantage":    "combat_tension",
    "emergency_response": "combat_tension",
    "flee":               "combat_tension",
    "defensive_action":   "combat_tension",
    "social_interaction": "tavern_chatter",
    "move_to_next_area":  "dungeon_ambience",
    "recover":            "campfire_quiet",
    "regroup":            "campfire_quiet",
}


class PropheticBuffer:
    """
    Fire-and-forget predictive pre-generation worker.

    Initialise once, call start() in the lifespan, then enqueue() after
    each completed PipelineResult.  All prefetch work happens in the
    background — the main pipeline is never blocked.
    """

    def __init__(
        self,
        cache:     "CacheService",
        storyteller: "GeminiClient | ClaudeClient",
    ) -> None:
        self._cache       = cache
        self._storyteller = storyteller
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._task: asyncio.Task | None = None
        self._busy        = False

    @property
    def is_busy(self) -> bool:
        return self._busy

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the background worker coroutine."""
        self._task = asyncio.create_task(self._worker(), name="prophetic-buffer")
        logger.info("PropheticBuffer worker started.")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Public Interface ──────────────────────────────────────────────────────

    async def enqueue(self, result: "PipelineResult") -> None:
        """
        Submit a completed PipelineResult for speculative prefetch.

        Non-blocking: drops silently if the queue is full (backpressure).
        """
        try:
            self._queue.put_nowait(result)
        except asyncio.QueueFull:
            logger.debug("PropheticBuffer queue full — prefetch dropped for intent %s",
                         result.intent.intent_id)

    # ── Background Worker ─────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            result = await self._queue.get()
            self._busy = True
            try:
                await self._prefetch(result)
            except Exception as exc:
                logger.debug("PropheticBuffer prefetch error (non-fatal): %s", exc)
            finally:
                self._busy = False
                self._queue.task_done()

    async def _prefetch(self, result: "PipelineResult") -> None:
        outcome     = result.resolution.outcome.value
        intent_id   = result.intent.intent_id
        action_type = result.resolution.action_type
        char_name   = result.narrative.narrative[:40]   # snippet for context

        follow_ups = _FOLLOW_UP_MAP.get(outcome, ["assess_situation"])
        primary    = follow_ups[0]

        # Pre-select ambient audio key
        audio_key = _AMBIENT_PREDICTION.get(primary)
        if audio_key:
            await self._cache_set(f"ironclad:prophet:{intent_id}:audio", audio_key)

        # Pre-generate a short narrative snippet for the predicted follow-up
        system = (
            "You are a Game Master preparing a short atmospheric bridge passage. "
            "Write 2 sentences of evocative scene-setting for the moment AFTER a player "
            f"action resolves as '{outcome}'. No dialogue, no stats, no names. "
            "Prose only, present tense."
        )
        user = (
            f"The last action was: {action_type}. "
            f"The scene began: \"{char_name}…\" "
            f"Write a 2-sentence atmospheric bridge for a {primary.replace('_', ' ')} follow-up."
        )

        try:
            async with asyncio.timeout(_PREFETCH_TIMEOUT):
                snippet = await self._storyteller.generate(
                    system_prompt=system,
                    user_prompt=user,
                    max_tokens=120,
                )
            if snippet:
                await self._cache_set(f"ironclad:prophet:{intent_id}:text", snippet)
                logger.debug(
                    "PropheticBuffer: prefetched snippet for intent %s (outcome=%s follow_up=%s)",
                    intent_id, outcome, primary,
                )
        except TimeoutError:
            logger.debug("PropheticBuffer: prefetch timed out for intent %s", intent_id)

    async def _cache_set(self, key: str, value: str) -> None:
        try:
            await self._cache.set(key, value, ttl=_PREFETCH_TTL)
        except Exception as exc:
            logger.debug("PropheticBuffer cache write failed: %s", exc)

    # ── Cache Read (called by pipeline to check for prefetched assets) ────────

    async def get_prefetched_text(self, intent_id: str) -> str | None:
        try:
            return await self._cache.get(f"ironclad:prophet:{intent_id}:text")
        except Exception:
            return None

    async def get_prefetched_audio(self, intent_id: str) -> str | None:
        try:
            return await self._cache.get(f"ironclad:prophet:{intent_id}:audio")
        except Exception:
            return None

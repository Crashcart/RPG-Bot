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

Idle Prefetch
-------------
A second background loop fires every _IDLE_INTERVAL seconds when no active
pipeline is running and no VoiceClient is in a voice channel.  It pre-generates
a set of generic warm-up assets (music clip, NPC portrait, scene image, recap
snippet, TTS clip) so the first turn after a long break skips the cold-start
penalty.  It also calls node_router.warmup_all_nodes() to keep Ollama models
loaded in VRAM.

Redis gate keys checked before each idle cycle:
    ironclad:sentinel:busy   — set during Phase 2 adjudication; skip if present.
    ironclad:voice:active    — set by voice_manager when in a voice channel;
                               skip if present (audio is already streaming).

Idle cache keys (TTL: _IDLE_TTL):
    ironclad:prophet:idle:text      — atmospheric recap snippet
    ironclad:prophet:idle:audio     — ambient SFX URL
    ironclad:prophet:idle:scene     — scene image URL
    ironclad:prophet:idle:portrait  — NPC portrait URL
    ironclad:prophet:idle:tts       — TTS narration URL
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.services.cache             import CacheService
    from orchestrator.services.gemini_client     import GeminiClient
    from orchestrator.services.claude_client     import ClaudeClient
    from orchestrator.services.node_router       import NodeRouter
    from orchestrator.services.elevenlabs_client import ElevenLabsClient
    from orchestrator.services.image_gen         import ImageGenService
    from orchestrator.schemas.payloads           import PipelineResult

logger = logging.getLogger(__name__)

_PREFETCH_TTL     = 120   # seconds — per-turn prefetch cache window
_PREFETCH_TIMEOUT = 20    # seconds per prefetch task
_MAX_QUEUE        = 64    # drop oldest if queue backs up

_IDLE_INTERVAL    = 300   # seconds between idle prefetch cycles (5 min)
_IDLE_TTL         = 600   # cache idle assets for 10 minutes
_IDLE_TIMEOUT     = 30    # seconds per idle generation task

# Fallback ElevenLabs "Rachel" voice — used for idle TTS when no
# campaign-specific voice is configured in system_settings.
_IDLE_NARRATOR_VOICE = "21m00Tcm4TlvDq8ikWAM"

# Generic prompts — no campaign context available during idle
_IDLE_SCENE_PROMPT = (
    "A dimly lit dungeon corridor, stone walls, flickering torch light, wisps of fog, "
    "fantasy RPG atmosphere, painterly digital art style."
)
_IDLE_NPC_PROMPT = (
    "Portrait of a hooded mysterious traveller, unknown origin, dramatic side-lighting, "
    "fantasy RPG art style, head and shoulders composition."
)
_IDLE_MUSIC_PROMPT  = "slow mysterious dungeon ambience, distant dripping water, low rumble, 10 seconds"
_IDLE_RECAP_SYSTEM  = (
    "You are an RPG Game Master. Write a 3-sentence atmospheric recap of a recent adventure — "
    "present tense, evocative prose, no character names or mechanical stats."
)
_IDLE_RECAP_USER    = "Write a short atmospheric 'previously on…' recap passage for a fantasy RPG session."
_IDLE_TTS_TEXT      = (
    "The torch gutters. Somewhere ahead, stone scrapes against stone. "
    "The party holds its breath."
)

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

    Optional multimedia services (node_router, elevenlabs, image_gen) enable
    the idle prefetch loop that warms up assets and Ollama VRAM between turns.
    """

    def __init__(
        self,
        cache:       "CacheService",
        storyteller: "GeminiClient | ClaudeClient",
        *,
        node_router: "NodeRouter | None"       = None,
        elevenlabs:  "ElevenLabsClient | None" = None,
        image_gen:   "ImageGenService | None"  = None,
    ) -> None:
        self._cache       = cache
        self._storyteller = storyteller
        self._node_router = node_router
        self._elevenlabs  = elevenlabs
        self._image_gen   = image_gen
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._task:      asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._busy = False

    @property
    def is_busy(self) -> bool:
        return self._busy

    # ── Lifecycle ──────────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the per-turn worker and (when multimedia services are wired) the idle prefetch loop."""
        self._task = asyncio.create_task(self._worker(), name="prophetic-buffer")
        _has_multimedia = (
            self._node_router is not None
            or self._elevenlabs is not None
            or self._image_gen is not None
        )
        if _has_multimedia:
            self._idle_task = asyncio.create_task(
                self._idle_prefetch_loop(), name="prophetic-buffer-idle"
            )
        logger.info(
            "PropheticBuffer started (idle prefetch: %s).",
            "enabled" if _has_multimedia else "disabled — multimedia services not wired",
        )

    async def stop(self) -> None:
        tasks = [t for t in (self._task, self._idle_task) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

    # ── Public Interface ──────────────────────────────────────────────────────────────────────────

    async def enqueue(self, result: "PipelineResult") -> None:
        """
        Submit a completed PipelineResult for speculative prefetch.

        Non-blocking: drops silently if the queue is full (backpressure).
        """
        try:
            self._queue.put_nowait(result)
        except asyncio.QueueFull:
            logger.debug(
                "PropheticBuffer queue full — prefetch dropped for intent %s",
                result.intent.intent_id,
            )

    async def run_idle_prefetch(self) -> None:
        """
        Pre-generate a set of warm-up assets during server idle time.

        Skips silently when:
          - A pipeline turn is in flight  (ironclad:sentinel:busy is set)
          - A VoiceClient is in a channel (ironclad:voice:active is set)
          - All optional multimedia services are absent

        Generated assets land in Redis under ``ironclad:prophet:idle:*``
        with a 10-minute TTL.  The first pipeline turn after an idle period
        can pull from this cache to eliminate cold-start latency.
        """
        try:
            if await self._cache.get("ironclad:sentinel:busy"):
                logger.debug("PropheticBuffer idle prefetch skipped — pipeline active.")
                return
            if await self._cache.get("ironclad:voice:active"):
                logger.debug("PropheticBuffer idle prefetch skipped — voice active.")
                return
        except Exception as exc:
            logger.debug("PropheticBuffer idle gate check failed: %s", exc)
            return

        tasks: list[asyncio.Task] = []

        if self._node_router is not None:
            tasks.append(asyncio.create_task(
                self._idle_warmup_nodes(), name="idle-warmup"
            ))

        tasks.append(asyncio.create_task(
            self._idle_recap(), name="idle-recap"
        ))

        if self._elevenlabs is not None:
            tasks.append(asyncio.create_task(
                self._idle_sfx(), name="idle-sfx"
            ))
            tasks.append(asyncio.create_task(
                self._idle_tts(), name="idle-tts"
            ))

        if self._image_gen is not None:
            tasks.append(asyncio.create_task(
                self._idle_scene_image(), name="idle-scene"
            ))
            tasks.append(asyncio.create_task(
                self._idle_portrait(), name="idle-portrait"
            ))

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        error_count = sum(1 for r in results if isinstance(r, Exception))
        logger.info(
            "PropheticBuffer idle prefetch done: %d tasks, %d errors.",
            len(tasks), error_count,
        )

    # ── Background Workers ──────────────────────────────────────────────────────────────────────

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

    async def _idle_prefetch_loop(self) -> None:
        while True:
            await asyncio.sleep(_IDLE_INTERVAL)
            self._busy = True
            try:
                await self.run_idle_prefetch()
            except Exception as exc:
                logger.debug("PropheticBuffer idle loop error (non-fatal): %s", exc)
            finally:
                self._busy = False

    # ── Per-Turn Prefetch ──────────────────────────────────────────────────────────────────────

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

    # ── Idle Prefetch Helpers ───────────────────────────────────────────────────────────────────

    async def _idle_warmup_nodes(self) -> None:
        try:
            async with asyncio.timeout(_IDLE_TIMEOUT):
                await self._node_router.warmup_all_nodes()
            logger.debug("PropheticBuffer: idle node warmup complete.")
        except (TimeoutError, Exception) as exc:
            logger.debug("PropheticBuffer: idle node warmup failed: %s", exc)

    async def _idle_recap(self) -> None:
        try:
            async with asyncio.timeout(_IDLE_TIMEOUT):
                snippet = await self._storyteller.generate(
                    system_prompt=_IDLE_RECAP_SYSTEM,
                    user_prompt=_IDLE_RECAP_USER,
                    max_tokens=100,
                )
            if snippet:
                await self._cache_set("ironclad:prophet:idle:text", snippet, ttl=_IDLE_TTL)
                logger.debug("PropheticBuffer: idle recap cached.")
        except (TimeoutError, Exception) as exc:
            logger.debug("PropheticBuffer: idle recap failed: %s", exc)

    async def _idle_sfx(self) -> None:
        try:
            async with asyncio.timeout(_IDLE_TIMEOUT):
                url = await self._elevenlabs.generate_sfx(
                    _IDLE_MUSIC_PROMPT, duration_seconds=10.0
                )
            if url:
                await self._cache_set("ironclad:prophet:idle:audio", url, ttl=_IDLE_TTL)
                logger.debug("PropheticBuffer: idle SFX cached.")
        except (TimeoutError, Exception) as exc:
            logger.debug("PropheticBuffer: idle SFX failed: %s", exc)

    async def _idle_tts(self) -> None:
        try:
            async with asyncio.timeout(_IDLE_TIMEOUT):
                url = await self._elevenlabs.generate_tts(
                    text=_IDLE_TTS_TEXT,
                    voice_id=_IDLE_NARRATOR_VOICE,
                )
            if url:
                await self._cache_set("ironclad:prophet:idle:tts", url, ttl=_IDLE_TTL)
                logger.debug("PropheticBuffer: idle TTS cached.")
        except (TimeoutError, Exception) as exc:
            logger.debug("PropheticBuffer: idle TTS failed: %s", exc)

    async def _idle_scene_image(self) -> None:
        try:
            async with asyncio.timeout(_IDLE_TIMEOUT):
                url = await self._image_gen.generate(_IDLE_SCENE_PROMPT)
            if url:
                await self._cache_set("ironclad:prophet:idle:scene", url, ttl=_IDLE_TTL)
                logger.debug("PropheticBuffer: idle scene image cached.")
        except (TimeoutError, Exception) as exc:
            logger.debug("PropheticBuffer: idle scene image failed: %s", exc)

    async def _idle_portrait(self) -> None:
        try:
            async with asyncio.timeout(_IDLE_TIMEOUT):
                url = await self._image_gen.generate_npc_portrait(
                    npc_name="Mysterious Stranger",
                    description="hooded traveller, unknown origin, dramatic side-lighting",
                    campaign_id="__idle_prefetch__",
                )
            if url:
                await self._cache_set("ironclad:prophet:idle:portrait", url, ttl=_IDLE_TTL)
                logger.debug("PropheticBuffer: idle portrait cached.")
        except (TimeoutError, Exception) as exc:
            logger.debug("PropheticBuffer: idle portrait failed: %s", exc)

    # ── Cache I/O ───────────────────────────────────────────────────────────────────────────────

    async def _cache_set(self, key: str, value: str, ttl: int = _PREFETCH_TTL) -> None:
        try:
            await self._cache.set(key, value, ttl=ttl)
        except Exception as exc:
            logger.debug("PropheticBuffer cache write failed: %s", exc)

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

    async def get_idle_prefetch(self, asset: str) -> str | None:
        """
        Read an idle-prefetched asset by name.

        Args:
            asset: One of ``text``, ``audio``, ``scene``, ``portrait``, ``tts``.

        Returns:
            The cached string (URL or text) or None if not yet generated / expired.
        """
        try:
            return await self._cache.get(f"ironclad:prophet:idle:{asset}")
        except Exception:
            return None

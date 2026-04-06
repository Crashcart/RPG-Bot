"""
Prophetic Buffer — Branching Narrative Orchestrator (Zero-Latency Engine)
=========================================================================
Speculative Narrative Pre-Computation: predicts the top-N most likely player
actions after each pipeline turn and pre-generates GM narrative responses in
the background, caching them in Redis with a configurable TTL (default 5 min).

When the player submits their next command, the pipeline calls
``get_speculative_response()`` which scores the input against each cached
branch with a fast keyword-match algorithm.  On a cache hit the expensive
cloud storyteller call is skipped entirely, yielding near-zero narration
latency.

Logic Flow
----------
1. **Intent Prediction** — after every completed ``PipelineResult``, analyse the
   scene (outcome, action_type) and predict the top-N likely follow-up action
   categories.

2. **Load Gating** — ``psutil`` (optional; falls back gracefully) reads host
   CPU and RAM usage.  Under moderate load the branch count is reduced to 2;
   under heavy load it falls to 1.  Generation is never fully suppressed so the
   engine always has at least one warm branch ready.

3. **Background Pre-computation** — one ``storyteller.generate()`` call fires
   per branch concurrently (bounded by ``asyncio.timeout``).  Results are
   serialised to JSON and stored under ``ironclad:speculative:{guild_id}`` with
   the configured TTL.

4. **Semantic Resolution** — ``get_speculative_response()`` decodes the cached
   branch list, scores the player's raw input against each branch's keyword set
   via a weighted keyword-overlap algorithm, and returns the narrative text of
   the highest-scoring branch if it exceeds the configured similarity threshold.
   The stale cache entry is immediately deleted on any resolution (hit or miss)
   so that old branches never bleed into the next turn.

5. **Cache Pruning** — Redis TTL guarantees automatic cleanup within 5 minutes.
   Explicit deletion on resolution ensures the engine starts fresh each turn.

Cache keys
----------
    ironclad:speculative:{guild_id}  → JSON array of BranchEntry dicts
TTL: ``settings.speculative_ttl_seconds`` (default 300 s)

Legacy prefetch keys (backward-compat)
---------------------------------------
    ironclad:prophet:{intent_id}:text   → pre-generated narrative snippet
    ironclad:prophet:{intent_id}:audio  → predicted ambient_audio_key string
These are retained for callers that still use the old ``get_prefetched_text``
and ``get_prefetched_audio`` API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from orchestrator.config               import Settings
    from orchestrator.services.cache       import CacheService
    from orchestrator.services.gemini_client import GeminiClient
    from orchestrator.services.claude_client import ClaudeClient
    from orchestrator.schemas.payloads     import PipelineResult

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_LEGACY_PREFETCH_TTL = 120   # seconds — legacy ironclad:prophet:* keys
_PREFETCH_TIMEOUT    = 20    # seconds — per-branch generation timeout
_MAX_QUEUE           = 64    # drop oldest if queue backs up

# ── Branch Prediction Tables ──────────────────────────────────────────────────

# Maps mechanical outcome → ordered list of likely follow-up action categories.
_FOLLOW_UP_MAP: dict[str, list[str]] = {
    "critical_success": ["press_advantage",    "loot_search",       "social_interaction"],
    "success":          ["press_advantage",    "move_to_next_area", "social_interaction"],
    "partial_success":  ["retry_skill",        "assess_situation",  "defensive_action"],
    "failure":          ["defensive_action",   "escape_attempt",    "regroup"],
    "critical_failure": ["emergency_response", "flee",              "call_for_help"],
}

# Maps branch label → set of keyword tokens players typically type for that action.
# Used by the keyword-overlap scorer in get_speculative_response().
_BRANCH_KEYWORDS: dict[str, frozenset[str]] = {
    "press_advantage": frozenset({
        "attack", "strike", "press", "advance", "charge", "swing", "slash",
        "stab", "hit", "fight", "kill", "assault", "aggress", "pursue",
        "keep", "push", "continue", "finish",
    }),
    "loot_search": frozenset({
        "search", "loot", "look", "examine", "check", "take", "grab", "find",
        "pick", "investigate", "inspect", "scan", "rummage", "rifled",
        "collect", "retrieve",
    }),
    "social_interaction": frozenset({
        "talk", "speak", "say", "ask", "tell", "negotiate", "persuade",
        "convince", "chat", "converse", "question", "threaten", "bribe",
        "intimidate", "bluff", "lie", "haggle",
    }),
    "move_to_next_area": frozenset({
        "move", "go", "walk", "proceed", "enter", "exit", "leave", "head",
        "travel", "advance", "cross", "open", "door", "gate", "pass",
        "continue", "follow",
    }),
    "escape_attempt": frozenset({
        "run", "flee", "escape", "retreat", "hide", "withdraw", "back",
        "away", "sprint", "bolt", "dash", "evade", "sneak",
    }),
    "defensive_action": frozenset({
        "defend", "block", "parry", "dodge", "shield", "protect", "brace",
        "guard", "duck", "deflect", "cover", "take", "ready",
    }),
    "recover": frozenset({
        "rest", "heal", "recover", "bandage", "treat", "tend", "sleep",
        "patch", "bind", "medicine", "potion", "drink", "use",
    }),
    "regroup": frozenset({
        "regroup", "gather", "plan", "strategy", "discuss", "regroup",
        "reposition", "rally", "huddle", "think", "consider", "wait",
    }),
    "retry_skill": frozenset({
        "try", "attempt", "retry", "again", "redo", "repeat",
        "another", "second", "once", "more",
    }),
    "assess_situation": frozenset({
        "look", "assess", "survey", "scan", "observe", "watch", "wait",
        "think", "study", "examine", "analyse", "analyze", "sense",
        "perception", "check", "notice",
    }),
    "emergency_response": frozenset({
        "help", "save", "revive", "stabilize", "stabilise", "heal",
        "medic", "aid", "assist", "rush", "quick",
    }),
    "flee": frozenset({
        "flee", "run", "escape", "sprint", "bolt", "dash", "jump",
        "dive", "leap", "abandon",
    }),
    "call_for_help": frozenset({
        "call", "shout", "yell", "scream", "shout", "summon", "signal",
        "help", "allies", "backup", "reinforce", "alert",
    }),
}

# Maps branch label → ambient audio key for the predicted scene atmosphere.
_AMBIENT_PREDICTION: dict[str, str] = {
    "press_advantage":    "combat_tension",
    "escape_attempt":     "combat_tension",
    "flee":               "combat_tension",
    "defensive_action":   "combat_tension",
    "emergency_response": "combat_tension",
    "social_interaction": "tavern_chatter",
    "move_to_next_area":  "dungeon_ambience",
    "recover":            "campfire_quiet",
    "regroup":            "campfire_quiet",
    "loot_search":        "dungeon_ambience",
}


# ── TypedDict for cached branch entries ──────────────────────────────────────

class BranchEntry(TypedDict):
    label:             str   # e.g. "press_advantage"
    narrative_text:    str   # pre-generated GM narrative prose
    ambient_audio_key: str   # pre-selected ambient audio key (may be empty)


# ── Main Class ────────────────────────────────────────────────────────────────

class PropheticBuffer:
    """
    Branching Narrative Orchestrator — Zero-Latency Engine.

    Initialise once at startup, call ``start()`` in the lifespan, then
    ``enqueue()`` after each completed ``PipelineResult``.  All branch
    pre-generation work runs in the background — the main pipeline is never
    blocked.

    Call ``get_speculative_response(guild_id, player_input)`` in the pipeline
    to attempt a cache hit before falling back to a full Phase 4 generation.
    """

    def __init__(
        self,
        cache:       "CacheService",
        storyteller: "GeminiClient | ClaudeClient",
        settings:    "Settings | None" = None,
    ) -> None:
        self._cache       = cache
        self._storyteller = storyteller
        self._settings    = settings
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
        logger.info("PropheticBuffer (Zero-Latency Engine) worker started.")

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
        Submit a completed PipelineResult for speculative branch pre-generation.

        Non-blocking: drops silently if the queue is full (backpressure).
        """
        try:
            self._queue.put_nowait(result)
        except asyncio.QueueFull:
            logger.debug(
                "PropheticBuffer queue full — speculative prefetch dropped for "
                "intent %s",
                result.intent.intent_id,
            )

    async def get_speculative_response(
        self,
        guild_id:    str,
        player_input: str,
    ) -> BranchEntry | None:
        """
        Attempt to resolve the player's input against a pre-computed branch.

        Returns the best-matching ``BranchEntry`` (containing ``narrative_text``
        and ``ambient_audio_key``) if the keyword-match score exceeds the
        configured threshold.  Returns ``None`` on a cache miss.

        Side-effect: the cached entry for this guild is deleted unconditionally
        after any call so that old branches never pollute the next turn.
        """
        if self._settings and not self._settings.speculative_engine_enabled:
            return None

        cache_key = self._speculative_key(guild_id)
        try:
            raw = await self._cache.get(cache_key)
        except Exception as exc:
            logger.debug("PropheticBuffer: Redis read error: %s", exc)
            return None
        finally:
            # Always flush stale branches — the next turn will repopulate.
            try:
                await self._cache.delete(cache_key)
            except Exception:
                pass

        if not raw:
            logger.debug(
                "PropheticBuffer: cache miss for guild %s (no branches stored)",
                guild_id,
            )
            return None

        try:
            branches: list[BranchEntry] = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("PropheticBuffer: corrupt cache entry: %s", exc)
            return None

        threshold = (
            self._settings.speculative_similarity_threshold
            if self._settings
            else 0.30
        )
        best_entry, best_score = self._best_match(player_input, branches)

        if best_entry and best_score >= threshold:
            logger.info(
                "PropheticBuffer: cache HIT for guild %s "
                "(branch=%s score=%.2f threshold=%.2f)",
                guild_id, best_entry["label"], best_score, threshold,
            )
            return best_entry

        logger.debug(
            "PropheticBuffer: cache MISS for guild %s "
            "(best_score=%.2f threshold=%.2f)",
            guild_id, best_score, threshold,
        )
        return None

    # ── Legacy Prefetch API (backward-compat) ─────────────────────────────────

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

    # ── Background Worker ─────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            result = await self._queue.get()
            self._busy = True
            try:
                await self._prefetch(result)
            except Exception as exc:
                logger.debug(
                    "PropheticBuffer prefetch error (non-fatal): %s", exc
                )
            finally:
                self._busy = False
                self._queue.task_done()

    async def _prefetch(self, result: "PipelineResult") -> None:
        """
        Core prefetch routine: predict branches, respect load limits, generate
        narrative text for each branch concurrently, then cache the results.
        """
        if self._settings and not self._settings.speculative_engine_enabled:
            return

        outcome     = result.resolution.outcome.value
        action_type = result.resolution.action_type
        guild_id    = result.intent.guild_id
        intent_id   = result.intent.intent_id

        # ── Determine how many branches to pre-generate ───────────────────────
        max_branches = self._effective_branch_count()
        if max_branches == 0:
            logger.debug(
                "PropheticBuffer: skipping prefetch (engine disabled by load "
                "or config) for intent %s",
                intent_id,
            )
            return

        # ── Predict branch labels ─────────────────────────────────────────────
        predicted_labels = _FOLLOW_UP_MAP.get(outcome, ["assess_situation"])
        labels_to_run    = predicted_labels[:max_branches]

        logger.debug(
            "PropheticBuffer: predicting %d branch(es) for intent %s "
            "(outcome=%s labels=%s)",
            len(labels_to_run), intent_id, outcome, labels_to_run,
        )

        # ── Generate narrative text for each branch concurrently ──────────────
        tasks = [
            self._generate_branch(label, outcome, action_type)
            for label in labels_to_run
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        branches: list[BranchEntry] = []
        for label, res in zip(labels_to_run, results):
            if isinstance(res, Exception):
                logger.debug(
                    "PropheticBuffer: branch '%s' generation failed: %s",
                    label, res,
                )
                continue
            if res:
                branches.append(res)

        if not branches:
            return

        # ── Store branches in Redis ───────────────────────────────────────────
        ttl = (
            self._settings.speculative_ttl_seconds if self._settings else 300
        )
        try:
            await self._cache.set(
                self._speculative_key(guild_id),
                json.dumps(branches),
                ttl=ttl,
            )
            logger.info(
                "PropheticBuffer: stored %d branch(es) for guild %s "
                "(intent=%s ttl=%ds)",
                len(branches), guild_id, intent_id, ttl,
            )
        except Exception as exc:
            logger.debug("PropheticBuffer: cache write failed: %s", exc)

        # ── Legacy prefetch keys (backward-compat) ────────────────────────────
        primary_label = labels_to_run[0]
        audio_key = _AMBIENT_PREDICTION.get(primary_label)
        if audio_key:
            try:
                await self._cache.set(
                    f"ironclad:prophet:{intent_id}:audio",
                    audio_key,
                    ttl=_LEGACY_PREFETCH_TTL,
                )
            except Exception:
                pass

        if branches:
            try:
                await self._cache.set(
                    f"ironclad:prophet:{intent_id}:text",
                    branches[0]["narrative_text"],
                    ttl=_LEGACY_PREFETCH_TTL,
                )
            except Exception:
                pass

    async def _generate_branch(
        self,
        label:       str,
        outcome:     str,
        action_type: str,
    ) -> BranchEntry | None:
        """
        Generate narrative prose for a single predicted branch via the cloud
        storyteller.  Returns None on timeout or generation failure.
        """
        ambient_audio_key = _AMBIENT_PREDICTION.get(label, "")
        action_label      = label.replace("_", " ")

        system = (
            "You are a Game Master writing a short, immersive narrative passage. "
            "Write 3–4 sentences of vivid, present-tense atmospheric prose for "
            f"the moment a player chooses to **{action_label}** immediately after "
            f"a **{outcome.replace('_', ' ')}** action resolves. "
            "Focus on sensory detail, momentum, and drama. "
            "No dialogue tags, no stats, no character names — pure scene prose."
        )
        user = (
            f"The previous action was: {action_type.replace('_', ' ')}. "
            f"Write a 3–4 sentence atmospheric narrative for a "
            f"'{action_label}' follow-up scene."
        )

        try:
            async with asyncio.timeout(_PREFETCH_TIMEOUT):
                text = await self._storyteller.generate(
                    system_prompt=system,
                    user_prompt=user,
                    max_tokens=180,
                )
        except TimeoutError:
            logger.debug(
                "PropheticBuffer: branch '%s' timed out after %ds",
                label, _PREFETCH_TIMEOUT,
            )
            return None
        except Exception as exc:
            logger.debug(
                "PropheticBuffer: branch '%s' generation error: %s", label, exc
            )
            return None

        if not text:
            return None

        return BranchEntry(
            label=label,
            narrative_text=text.strip(),
            ambient_audio_key=ambient_audio_key,
        )

    # ── Semantic Resolution ────────────────────────────────────────────────────

    @staticmethod
    def _tokenise(text: str) -> frozenset[str]:
        """
        Lowercase and extract word tokens from text, stripping stopwords.
        """
        _STOPWORDS = frozenset({
            "i", "my", "the", "a", "an", "to", "and", "or", "is", "are", "in",
            "of", "for", "with", "at", "by", "do", "it", "up", "on", "be",
            "will", "that", "this", "as", "so", "if", "not", "but", "was",
            "have", "from", "into", "out", "its", "am", "he", "she", "we",
            "they", "his", "her", "our", "their", "you", "your",
        })
        words = re.findall(r"[a-z]+", text.lower())
        return frozenset(w for w in words if w not in _STOPWORDS)

    @classmethod
    def _score_branch(cls, player_tokens: frozenset[str], label: str) -> float:
        """
        Compute a precision-based keyword-overlap score.

        Score = matched_keywords / len(player_tokens)  (0.0 – 1.0)

        Precision answers "what fraction of what the player typed maps to this
        branch?" which is the right question for short, focused player inputs
        (typically 3–8 meaningful tokens).  This is far more sensitive than
        recall (matched / keywords) for large keyword sets.
        """
        if not player_tokens:
            return 0.0
        keywords = _BRANCH_KEYWORDS.get(label, frozenset())
        if not keywords:
            return 0.0
        matched = player_tokens & keywords
        return len(matched) / len(player_tokens)

    @classmethod
    def _best_match(
        cls,
        player_input: str,
        branches:     list[BranchEntry],
    ) -> tuple[BranchEntry | None, float]:
        """
        Return the (best_branch, best_score) tuple for the given player input.
        """
        if not branches:
            return None, 0.0

        player_tokens = cls._tokenise(player_input)
        best_entry: BranchEntry | None = None
        best_score: float = 0.0

        for entry in branches:
            score = cls._score_branch(player_tokens, entry["label"])
            if score > best_score:
                best_score = score
                best_entry = entry

        return best_entry, best_score

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _speculative_key(guild_id: str) -> str:
        return f"ironclad:speculative:{guild_id}"

    def _effective_branch_count(self) -> int:
        """
        Query host load via psutil and return how many branches to generate.

        Falls back to the configured maximum when psutil is unavailable.
        Gracefully handles any psutil error as a no-op (uses configured max).
        """
        max_b = self._settings.speculative_branches if self._settings else 3

        try:
            import psutil  # optional dependency
            cpu = psutil.cpu_percent(interval=0.05)
            ram = psutil.virtual_memory().percent

            cpu_disable = (
                self._settings.speculative_cpu_disable if self._settings else 85
            )
            cpu_scale   = (
                self._settings.speculative_cpu_scale_down if self._settings else 70
            )
            ram_disable = (
                self._settings.speculative_ram_disable if self._settings else 90
            )
            ram_scale   = (
                self._settings.speculative_ram_scale_down if self._settings else 80
            )

            if cpu >= cpu_disable or ram >= ram_disable:
                logger.debug(
                    "PropheticBuffer: heavy load (cpu=%.0f%% ram=%.0f%%) "
                    "— reducing to 1 branch",
                    cpu, ram,
                )
                return min(1, max_b)

            if cpu >= cpu_scale or ram >= ram_scale:
                logger.debug(
                    "PropheticBuffer: moderate load (cpu=%.0f%% ram=%.0f%%) "
                    "— reducing to 2 branches",
                    cpu, ram,
                )
                return min(2, max_b)

        except ImportError:
            pass  # psutil not installed — use configured max
        except Exception as exc:
            logger.debug("PropheticBuffer: psutil error (non-fatal): %s", exc)

        return max_b

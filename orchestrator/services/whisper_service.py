"""Whisper Protocol — Hidden Psychological State Management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.services.database import DatabaseService

logger = logging.getLogger(__name__)

_SANITY_WARNING_THRESHOLD    = 40
_SANITY_PARANOIA_THRESHOLD   = 20
_SANITY_BREAKDOWN_THRESHOLD  = 5
_RATE_LIMIT_KEY              = "whisper:perception:{character_id}"
_RATE_LIMIT_MAX              = 3
_RATE_LIMIT_WINDOW_S         = 60

_HORROR_ACTION_TYPES: frozenset[str] = frozenset({
    "cursed_contact",
    "horror_witness",
    "sanity_check",
    "eldritch_gaze",
    "mind_assault",
    "possession_attempt",
    "void_exposure",
    "necrotic_touch",
    "psychic_damage",
    "fear_check",
    "madness_check",
})

_HORROR_KEYWORDS: tuple[str, ...] = (
    "sanity", "horror", "madness", "paranoi", "cursed", "eldritch",
    "dread", "terror", "possession", "void", "cosmic", "fear",
    "hallucin", "illusion", "psychic", "unnatural",
)


class WhisperService:
    """
    Evaluates whether a player action warrants a secret GM whisper
    based on action type, outcome keywords, and current sanity level.

    Rate-limited via Redis: at most 3 perception checks per character per 60 s.
    All rate-limit failures are open (whisper fires) to avoid silent drops.
    """

    def __init__(self, db: "DatabaseService", redis=None) -> None:
        self._db    = db
        self._redis = redis

    async def should_trigger_whisper(
        self,
        character_id: str,
        action_type:  str,
        reasoning:    str,
        outcome:      str,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Decide whether to trigger a whisper this turn.

        Returns (triggered: bool, hidden_state: dict).
        hidden_state is always the current DB value; it is populated even when
        triggered is False so callers can inspect it without an extra query.
        """
        try:
            hidden_state = await self._db.get_hidden_state(character_id)
        except Exception as exc:
            logger.debug("WhisperService: get_hidden_state failed (fail-open): %s", exc)
            hidden_state = {}

        triggered = self._evaluate_trigger(action_type, reasoning, outcome, hidden_state)

        if not triggered:
            return False, hidden_state

        # Redis rate-limit: fail-open on any Redis error
        if self._redis is not None:
            try:
                triggered = await self._check_rate_limit(character_id)
            except Exception as exc:
                logger.debug("WhisperService: rate-limit check failed (fail-open): %s", exc)
                triggered = True

        return triggered, hidden_state

    def _evaluate_trigger(
        self,
        action_type:  str,
        reasoning:    str,
        outcome:      str,
        hidden_state: dict[str, Any],
    ) -> bool:
        """Pure evaluation — no I/O."""
        if action_type.lower() in _HORROR_ACTION_TYPES:
            return True

        text = f"{reasoning} {outcome}".lower()
        if any(kw in text for kw in _HORROR_KEYWORDS):
            return True

        sanity = int(hidden_state.get("sanity", 100))
        if sanity <= _SANITY_WARNING_THRESHOLD:
            return True

        flags: list[str] = hidden_state.get("flags", [])
        if any(f in flags for f in ("paranoid", "cursed", "possessed")):
            return True

        return False

    async def _check_rate_limit(self, character_id: str) -> bool:
        """Increment the per-character perception counter; return True if under limit."""
        key = _RATE_LIMIT_KEY.format(character_id=character_id)
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, _RATE_LIMIT_WINDOW_S)
        return count <= _RATE_LIMIT_MAX

    async def apply_delta(
        self,
        character_id:  str,
        intent_id:     str,
        sanity_drain:  int        = 0,
        flags_add:     list[str]  | None = None,
        flags_remove:  list[str]  | None = None,
        trigger:       str        = "whisper_protocol",
        whisper_text:  str | None = None,
    ) -> dict[str, Any]:
        """
        Apply a hidden state mutation and log the delivery.
        Returns the post-commit hidden_state dict.
        """
        post_state = await self._db.apply_hidden_state_delta(
            character_id=character_id,
            sanity_drain=sanity_drain,
            flags_add=flags_add,
            flags_remove=flags_remove,
        )
        await self._db.log_whisper(
            character_id=character_id,
            intent_id=intent_id,
            trigger=trigger,
            delta={
                "sanity_drain": sanity_drain,
                "flags_add":    flags_add or [],
                "flags_remove": flags_remove or [],
            },
            whisper_text=whisper_text,
        )
        return post_state

    def build_hidden_context(self, hidden_state: dict[str, Any]) -> str:
        """
        Format the hidden psychological state into a terse GM context block
        injected into the whisper generation prompt.
        """
        if not hidden_state:
            return ""

        sanity = int(hidden_state.get("sanity", 100))
        flags: list[str] = hidden_state.get("flags", [])

        lines: list[str] = []
        if sanity <= _SANITY_BREAKDOWN_THRESHOLD:
            lines.append(f"Sanity: {sanity}/100 [BREAKDOWN IMMINENT]")
        elif sanity <= _SANITY_PARANOIA_THRESHOLD:
            lines.append(f"Sanity: {sanity}/100 [PARANOID]")
        elif sanity <= _SANITY_WARNING_THRESHOLD:
            lines.append(f"Sanity: {sanity}/100 [DETERIORATING]")

        if flags:
            lines.append(f"Active flags: {', '.join(flags)}")

        if not lines:
            return ""

        return "[HIDDEN PSYCHOLOGICAL CONTEXT — GM EYES ONLY]\n" + "\n".join(lines)

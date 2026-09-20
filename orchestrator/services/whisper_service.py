"""
Whisper Protocol — Hidden Psychological State & Perception Triggers
====================================================================
Implements the asymmetric information layer described in Issue #19.

The WhisperService tracks per-character sanity, fear, and status flags in the
`hidden_state` JSONB column (migration 014) and decides when the GM Director
should fork its narrative: a public scene for the full channel, and a private
hallucination whispered only to the affected player.

Key guarantees
--------------
* The player never sees their exact sanity score or flag state.
* Redis rate-limits hidden perception checks (3 per character per 60 s).
  The rate-limiter fails open — a Redis failure never blocks the pipeline.
* All DB writes are atomic (SELECT … FOR UPDATE) and fully parameterised.
* The LLM is never invoked from this service — it only mutates DB state and
  provides context strings for GMDirector's whisper generation pass.

Sanity thresholds (configurable via constants)
----------------------------------------------
  SANITY_FULL      = 100   (default starting sanity)
  SANITY_WARNING   = 40    → low-sanity flag set; GM whispers become bleaker
  SANITY_PARANOID  = 20    → 'paranoid' flag; contradictory hallucinations begin
  SANITY_BREAKDOWN = 5     → 'breakdown' flag; GM may describe full delusions
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.services.database import DatabaseService

from orchestrator.schemas.payloads import HiddenStateDelta

logger = logging.getLogger(__name__)

# ── Sanity thresholds ─────────────────────────────────────────────────────────
SANITY_WARNING   = 40
SANITY_PARANOID  = 20
SANITY_BREAKDOWN = 5

# ── Horror action types that may trigger a whisper check ─────────────────────
_HORROR_ACTION_TYPES: frozenset[str] = frozenset({
    "sanity_check",
    "eldritch_gaze",
    "horror_witness",
    "cursed_touch",
    "void_contact",
    "fear_check",
    "madness_trigger",
    "corruption_check",
    "dark_ritual",
    "psychic_assault",
})

# ── Outcome keywords in the reasoning field that can upgrade to horror ────────
_HORROR_KEYWORDS: frozenset[str] = frozenset({
    "sanity",
    "horror",
    "madness",
    "terrified",
    "eldritch",
    "cursed",
    "possessed",
    "insane",
    "corrupted",
    "hallucination",
    "void",
    "cosmic",
})

# ── Redis rate-limit config ───────────────────────────────────────────────────
_RATE_LIMIT_MAX   = 3    # max hidden checks per window
_RATE_LIMIT_TTL   = 60   # seconds


class WhisperService:
    """
    Manages hidden psychological state and decides when to fork the GM narrative
    into a public scene + private whisper for the affected player.
    """

    def __init__(self, db: "DatabaseService", redis=None) -> None:
        self._db    = db
        self._redis = redis   # aioredis client or compatible; None → rate-limit disabled

    # ── Public API ─────────────────────────────────────────────────────────────

    def should_trigger_whisper(
        self,
        action_type:  str,
        reasoning:    str,
        outcome:      str,
        hidden_state: dict[str, Any],
    ) -> bool:
        """
        Return True when this action warrants a hidden perception check.

        Triggers when any of:
          1. action_type is in the horror set
          2. reasoning/outcome contains horror keywords
          3. character already has 'paranoid', 'cursed', or 'possessed' flag
          4. sanity is at or below SANITY_WARNING threshold
        """
        if action_type in _HORROR_ACTION_TYPES:
            return True

        combined = f"{reasoning} {outcome}".lower()
        if any(kw in combined for kw in _HORROR_KEYWORDS):
            return True

        if hidden_state.get("paranoid") or hidden_state.get("cursed") or hidden_state.get("possessed"):
            return True

        sanity = hidden_state.get("sanity", 100)
        if isinstance(sanity, (int, float)) and sanity <= SANITY_WARNING:
            return True

        return False

    async def check_rate_limit(self, character_id: str) -> bool:
        """
        Return True if the character is allowed another hidden check this window.
        Fails open (returns True) when Redis is unavailable.
        """
        if self._redis is None:
            return True
        key = f"whisper:rl:{character_id}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, _RATE_LIMIT_TTL)
            return count <= _RATE_LIMIT_MAX
        except Exception as exc:
            logger.debug("WhisperService rate-limit Redis error (fail-open): %s", exc)
            return True

    async def apply_delta(
        self,
        character_id: str,
        campaign_id:  str,
        intent_id:    str,
        delta:        HiddenStateDelta,
        whisper_text: str | None = None,
    ) -> dict[str, Any]:
        """
        Atomically commit a HiddenStateDelta to the character's hidden_state.

        Steps:
          1. SELECT … FOR UPDATE to prevent concurrent mutations
          2. Apply sanity_delta and flag changes in Python
          3. Recompute threshold flags (low_sanity, paranoid, breakdown)
          4. UPDATE characters SET hidden_state = $new
          5. INSERT into whisper_log

        Returns the updated hidden_state dict.
        """
        from uuid import UUID

        pool = self._db.pool
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Fetch with row lock
                row = await conn.fetchrow(
                    "SELECT hidden_state FROM characters WHERE id = $1 FOR UPDATE",
                    UUID(character_id),
                )
                if row is None:
                    logger.warning("WhisperService.apply_delta: character %s not found", character_id)
                    return {}

                import json as _json
                hs: dict[str, Any] = (
                    _json.loads(row["hidden_state"])
                    if isinstance(row["hidden_state"], str)
                    else dict(row["hidden_state"] or {})
                )

                # 2. Apply sanity drain
                if delta.sanity_delta != 0:
                    current = hs.get("sanity", 100)
                    hs["sanity"] = max(0, min(100, current + delta.sanity_delta))

                # 3. Set / clear flags
                for flag, val in delta.flags_set.items():
                    hs[flag] = val
                for flag in delta.flags_cleared:
                    hs.pop(flag, None)

                # 4. Recompute threshold flags
                sanity = hs.get("sanity", 100)
                hs["low_sanity"]  = sanity <= SANITY_WARNING
                hs["paranoid"]    = hs.get("paranoid", False) or sanity <= SANITY_PARANOID
                hs["breakdown"]   = sanity <= SANITY_BREAKDOWN

                # 5. Persist
                await conn.execute(
                    "UPDATE characters SET hidden_state = $1 WHERE id = $2",
                    _json.dumps(hs),
                    UUID(character_id),
                )

                # 6. Audit log
                await conn.execute(
                    """
                    INSERT INTO whisper_log
                        (character_id, campaign_id, intent_id, trigger_reason,
                         sanity_delta, flags_applied, whisper_text)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    UUID(character_id),
                    UUID(campaign_id),
                    UUID(intent_id),
                    delta.trigger_reason or "unknown",
                    delta.sanity_delta,
                    _json.dumps({**delta.flags_set}),
                    whisper_text,
                )

        return hs

    def build_hidden_context(self, hidden_state: dict[str, Any]) -> str:
        """
        Format the character's psychological state into a GM-eyes-only context
        block to prepend to the whisper generation prompt.

        Never shown to the player; only the GM's whisper generation sees it.
        """
        if not hidden_state:
            return ""

        lines: list[str] = ["[HIDDEN PSYCHOLOGICAL STATE — GM EYES ONLY]"]

        sanity = hidden_state.get("sanity", 100)
        lines.append(f"Sanity: {sanity}/100")

        active_flags = [
            k for k, v in hidden_state.items()
            if v is True and k not in ("low_sanity",)
        ]
        if active_flags:
            lines.append(f"Active conditions: {', '.join(active_flags)}")

        if sanity <= SANITY_BREAKDOWN:
            lines.append("Severity: BREAKDOWN — full hallucinations are appropriate.")
        elif sanity <= SANITY_PARANOID:
            lines.append("Severity: PARANOID — contradictory, deeply unsettling perceptions.")
        elif sanity <= SANITY_WARNING:
            lines.append("Severity: LOW SANITY — creeping dread and misread details.")

        return "\n".join(lines)

    async def get_hidden_state(self, character_id: str) -> dict[str, Any]:
        """Fetch the current hidden_state for a character. Returns {} on miss."""
        from uuid import UUID
        import json as _json

        row = await self._db.pool.fetchrow(
            "SELECT hidden_state FROM characters WHERE id = $1",
            UUID(character_id),
        )
        if row is None:
            return {}
        raw = row["hidden_state"]
        if isinstance(raw, str):
            return _json.loads(raw)
        return dict(raw or {})

    async def get_recent_whispers(
        self,
        character_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Return the most recent whisper_log entries for a character.
        Used for catch-up context in long sessions.
        """
        from uuid import UUID

        rows = await self._db.pool.fetch(
            """
            SELECT trigger_reason, sanity_delta, flags_applied, whisper_text, delivered_at
            FROM whisper_log
            WHERE character_id = $1
            ORDER BY delivered_at DESC
            LIMIT $2
            """,
            UUID(character_id),
            limit,
        )
        return [dict(r) for r in rows]

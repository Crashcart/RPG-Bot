"""
ImmersionFilter — Centralised LLM Output Post-Processor
========================================================
Applies three independent transforms to generated text before it reaches
the player, and manages the character-sheet render gate.

Transforms (applied in this order by ``process()``):
  1. Brand scrubbing   — replace real-world brand/IP names with ``[???]``
  2. Structural strip  — remove markdown headings, dividers, bullet lists
  3. Censorship revert — remove asterisk-masking artefacts (``f***`` → ``f…``)
  4. Markdown flatten  — convert residual bullet/numbered lists to prose

Character-Sheet Gate:
  ``should_render_sheet()`` returns ``True`` only when the player's stat
  block actually changed since the last turn, using an MD5 content hash.
  Hashes are stored in-process (per player_id).  Pass a Redis client at
  construction time to share state across workers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from orchestrator.prompts.gm_prompts import BRAND_BLOCKLIST, STRUCTURAL_PATTERNS

logger = logging.getLogger(__name__)

# ── Compiled patterns ─────────────────────────────────────────────────────────

# Matches asterisk-masked words: one or more letters, two or more asterisks,
# optional trailing letters (e.g.  f***,  s**t,  b*****d).
_CENSORSHIP_RE = re.compile(r"([A-Za-z]+)\*{2,}([A-Za-z]*)")

# Numbered list items at line start (e.g. "1. Something")
_NUMBERED_LIST_RE = re.compile(r"(?m)^(\d+)\.\s+(.+)$")

# Bullet list items at line start (- item, * item, • item)
_BULLET_LIST_RE = re.compile(r"(?m)^[-*•]\s+(.+)$")

# Compile brand patterns once at import time for O(n·m) → one-pass matching.
# We use a single alternation regex so each output string is scanned once.
_BRAND_RE = re.compile(
    "(" + "|".join(re.escape(b) for b in sorted(BRAND_BLOCKLIST, key=len, reverse=True)) + ")",
    re.IGNORECASE,
)


# ── ImmersionFilter ───────────────────────────────────────────────────────────

class ImmersionFilter:
    """
    Stateless text transforms + stateful per-player sheet-diff gate.

    Parameters
    ----------
    redis_client:
        Optional async Redis client.  When supplied, ``should_render_sheet``
        stores hashes in Redis (key ``immersion:sheet:{player_id}``) so the
        gate works correctly across multiple workers.  When *None*, an
        in-process dict is used (suitable for single-process deployments).
    """

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client
        self._local_hashes: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def process(self, text: str) -> tuple[str, dict[str, Any]]:
        """
        Run all text transforms in sequence.

        Returns
        -------
        (cleaned_text, report)
            ``report`` is a dict summarising what was changed, useful for
            telemetry/logging.
        """
        report: dict[str, Any] = {}

        text, brand_count = self.scrub_brands(text)
        report["brands_scrubbed"] = brand_count

        # Flatten lists before stripping structural markers so list content
        # is preserved as prose rather than losing the first character of each
        # item to the bullet-pattern substitution.
        text, flatten_count = self.flatten_markdown_lists(text)
        report["lists_flattened"] = flatten_count

        text, structural_count = self.strip_structural(text)
        report["structural_stripped"] = structural_count

        text, censor_count = self.revert_censorship(text)
        report["censorship_reverted"] = censor_count

        return text, report

    # ── Transform: brand scrubbing ────────────────────────────────────────────

    def scrub_brands(self, text: str) -> tuple[str, int]:
        """
        Replace all real-world brand names with ``[???]``.

        Returns ``(cleaned_text, match_count)``.  A non-zero count indicates
        a brand violation; callers should set ``brand_violation=True`` on the
        associated payload.
        """
        matches: list[str] = []

        def _replace(m: re.Match) -> str:
            matches.append(m.group(0))
            return "[???]"

        cleaned = _BRAND_RE.sub(_replace, text)
        if matches:
            logger.debug("ImmersionFilter: scrubbed %d brand(s): %s", len(matches), matches)
        return cleaned, len(matches)

    def detect_brand(self, text: str) -> str | None:
        """Return the first brand name found (case-insensitive), or ``None``."""
        m = _BRAND_RE.search(text)
        return m.group(0).lower() if m else None

    # ── Transform: structural text strip ─────────────────────────────────────

    def strip_structural(self, text: str) -> tuple[str, int]:
        """
        Remove markdown headings, dividers, and list structure markers.

        Content of list items is preserved (only the leading bullet/number
        character is removed — use ``flatten_markdown_lists`` *before* this
        to convert list items to inline prose first).

        Returns ``(cleaned_text, patterns_removed)``.
        """
        stripped = 0
        for pattern in STRUCTURAL_PATTERNS:
            new_text, count = pattern.subn("", text)
            if count:
                stripped += count
                text = new_text
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, stripped

    # ── Transform: censorship reversion ──────────────────────────────────────

    def revert_censorship(self, text: str) -> tuple[str, int]:
        """
        Remove asterisk-masking artefacts left by over-cautious LLMs.

        ``f***`` → ``f…``   (stem preserved, elided suffix indicated)
        ``s**t`` → ``s…t``

        Returns ``(cleaned_text, occurrences_fixed)``.
        """
        count = 0

        def _fix(m: re.Match) -> str:
            nonlocal count
            count += 1
            stem = m.group(1)
            tail = m.group(2)
            return f"{stem}…{tail}" if tail else f"{stem}…"

        return _CENSORSHIP_RE.sub(_fix, text), count

    # ── Transform: markdown list flattening ──────────────────────────────────

    def flatten_markdown_lists(self, text: str) -> tuple[str, int]:
        """
        Convert bullet and numbered lists to flowing prose.

        Consecutive list items are joined with a comma-space separator;
        the last item ends with a period if the paragraph does not already
        contain terminal punctuation.

        Returns ``(cleaned_text, list_blocks_flattened)``.
        """
        count = 0

        # Flatten numbered lists first (they interleave least with prose)
        def _flatten_numbered(m: re.Match) -> str:
            nonlocal count
            count += 1
            return m.group(2)

        text = _NUMBERED_LIST_RE.sub(_flatten_numbered, text)

        # Flatten bullet lists
        def _flatten_bullet(m: re.Match) -> str:
            nonlocal count
            count += 1
            return m.group(1)

        text = _BULLET_LIST_RE.sub(_flatten_bullet, text)

        # Collapse consecutive non-empty lines that were list items into
        # a single prose paragraph separated by ", ".
        if count:
            text = re.sub(r"([^\n.!?])\n([^\n])", r"\1, \2", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

        return text, count

    # ── Sheet-diff gate ───────────────────────────────────────────────────────

    async def should_render_sheet(self, player_id: str, stats: dict[str, Any]) -> bool:
        """
        Return ``True`` iff the player's stat block changed since last turn.

        The first call for a given player_id is always ``True``.

        Side-effect: updates the stored hash to ``stats``.
        """
        new_hash = _hash_stats(stats)
        old_hash = await self._get_hash(player_id)
        if old_hash == new_hash:
            return False
        await self._set_hash(player_id, new_hash)
        return True

    def should_render_sheet_sync(self, player_id: str, stats: dict[str, Any]) -> bool:
        """Synchronous variant for use in non-async contexts."""
        new_hash = _hash_stats(stats)
        old_hash = self._local_hashes.get(player_id)
        if old_hash == new_hash:
            return False
        self._local_hashes[player_id] = new_hash
        return True

    def invalidate_sheet_cache(self, player_id: str) -> None:
        """Force next ``should_render_sheet`` call for this player to return True."""
        self._local_hashes.pop(player_id, None)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_hash(self, player_id: str) -> str | None:
        if self._redis is not None:
            try:
                val = await self._redis.get(f"immersion:sheet:{player_id}")
                return val.decode() if isinstance(val, bytes) else val
            except Exception:
                logger.debug("ImmersionFilter: Redis get failed for %s, using local", player_id)
        return self._local_hashes.get(player_id)

    async def _set_hash(self, player_id: str, h: str) -> None:
        self._local_hashes[player_id] = h
        if self._redis is not None:
            try:
                await self._redis.set(f"immersion:sheet:{player_id}", h)
            except Exception:
                logger.debug("ImmersionFilter: Redis set failed for %s", player_id)


# ── Utility ───────────────────────────────────────────────────────────────────

def _hash_stats(stats: dict[str, Any]) -> str:
    """Stable MD5 of a JSON-serialised stats dict (keys sorted)."""
    return hashlib.md5(
        json.dumps(stats, sort_keys=True, default=str).encode()
    ).hexdigest()

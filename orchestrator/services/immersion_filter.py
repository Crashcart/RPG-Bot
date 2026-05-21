"""
Immersion Filter — Post-Generation Text Scrubber & Character-Sheet UI Gate
==========================================================================
Enforces strict narrative immersion by post-processing every LLM output
before it reaches the player, and gates the Discord character-sheet embed
so it only renders when the player's state actually changed.

Applied by GMDirector as Step 4e — after structural text filtering (Step 4d)
and before the Paradox Engine (Step 4f).

Three ordered passes
--------------------
Pass 1 — Censorship Reversion
    Detects and expands asterisk self-censorship patterns produced by
    safety-tuned LLMs (e.g. "f**k" → "fuck", "b*tch" → "bitch").
    Unknown intra-word asterisks are stripped so partial words still read.

Pass 2 — Markdown List / Table Flattening
    Accidental bullet lists, numbered lists, and Markdown tables in prose
    are detected and rewritten as flowing, comma/semicolon-joined sentences.
    Complements the structural-header stripping in Step 4d.

Pass 3 — Brand Name Nullification
    A last-resort pass over the fully synthesised narrative. Catches
    prohibited real-world brand names that leaked through synthesis prompt
    instructions. Blocked terms are replaced with configurable lore-friendly
    substitutions or the default sentinel "[???]".

Character-Sheet Gate (TDR §3C)
------------------------------
StateCommitPayload carries pre_state and post_state dicts. The filter
computes a SHA-256 hash of each and compares them:
  - hashes differ  → render_character_sheet = True
  - hashes match   → render_character_sheet = False

This prevents the Discord bot from spamming the character-sheet embed
on turns where no stat or inventory value changed.

Integration
-----------
Instantiated once in main.py and injected into GMDirector via the optional
``immersion_filter`` constructor parameter. When None (e.g. unit tests) the
GMDirector falls back to the simpler state-delta logic already in place.

To wire up::

    from orchestrator.services.immersion_filter import ImmersionFilter
    immersion_filter = ImmersionFilter()
    gm_director = GMDirector(..., immersion_filter=immersion_filter)

For per-campaign custom blocklists::

    settings = await db.get_campaign_immersion_settings(campaign_id)
    immersion_filter = ImmersionFilter(extra_blocklist=settings.custom_blocklist)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from typing import Sequence

from orchestrator.prompts.gm_prompts import BRAND_BLOCKLIST

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Filter Report
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class FilterReport:
    """Structured telemetry emitted after each scrub_narrative() call."""
    censorship_reversions: int = 0
    list_flattenings:      int = 0
    brand_nullifications:  int = 0

    def any_applied(self) -> bool:  # noqa: D401
        return bool(
            self.censorship_reversions
            or self.list_flattenings
            or self.brand_nullifications
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "censorship_reversions": self.censorship_reversions,
            "list_flattenings":      self.list_flattenings,
            "brand_nullifications":  self.brand_nullifications,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1: Censorship Reversion
# ─────────────────────────────────────────────────────────────────────────────
# Maps compiled patterns → their uncensored forms.
# More specific patterns must appear before more general ones.
# Only include patterns where the full word can be inferred unambiguously.

_CENSORED_WORD_MAP: list[tuple[re.Pattern[str], str]] = [
    # 4-letter words
    (re.compile(r"\bf\*{2}k\b",         re.IGNORECASE), "fuck"),
    (re.compile(r"\bf\*ck\b",           re.IGNORECASE), "fuck"),
    (re.compile(r"\bsh\*t\b",           re.IGNORECASE), "shit"),
    (re.compile(r"\bs\*{2}t\b",         re.IGNORECASE), "shit"),
    (re.compile(r"\bd\*{2}k\b",         re.IGNORECASE), "dick"),
    (re.compile(r"\bc\*{2}k\b",         re.IGNORECASE), "cock"),
    (re.compile(r"\bc\*ck\b",           re.IGNORECASE), "cock"),
    (re.compile(r"\bp\*ss\b",           re.IGNORECASE), "piss"),
    (re.compile(r"\bd\*mn\b",           re.IGNORECASE), "damn"),
    (re.compile(r"\bh\*ll\b",           re.IGNORECASE), "hell"),
    (re.compile(r"\bcr\*p\b",           re.IGNORECASE), "crap"),
    (re.compile(r"\ba\*{2}\b",          re.IGNORECASE), "ass"),
    # 5-letter words
    (re.compile(r"\bb\*tch\b",          re.IGNORECASE), "bitch"),
    (re.compile(r"\bc\*{2}nt\b",        re.IGNORECASE), "cunt"),
    (re.compile(r"\bwh\*re\b",          re.IGNORECASE), "whore"),
    (re.compile(r"\bp\*ssy\b",          re.IGNORECASE), "pussy"),
    (re.compile(r"\bp\*{2}sy\b",        re.IGNORECASE), "pussy"),
    # 6+ letter words
    (re.compile(r"\bb\*st\*rd\b",       re.IGNORECASE), "bastard"),
    (re.compile(r"\bb\*stard\b",        re.IGNORECASE), "bastard"),
    (re.compile(r"\ba\*{2}hole\b",      re.IGNORECASE), "asshole"),
    (re.compile(r"\ba\*shole\b",        re.IGNORECASE), "asshole"),
    (re.compile(r"\bmotherf\*{2}ker\b", re.IGNORECASE), "motherfucker"),
    (re.compile(r"\bmotherf\*cker\b",   re.IGNORECASE), "motherfucker"),
    (re.compile(r"\bbl\*{2}dy\b",       re.IGNORECASE), "bloody"),
    (re.compile(r"\bbl\*ody\b",         re.IGNORECASE), "bloody"),
]

# Fallback: strip asterisks between letter characters without a known expansion
_INTRAWORD_ASTERISK = re.compile(r"(?<=[a-zA-Z])\*+(?=[a-zA-Z])")


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2: Markdown List / Table Flattening
# ─────────────────────────────────────────────────────────────────────────────

_BULLET_BLOCK    = re.compile(r"((?:^[ \t]*[-*•]\s+.+\n?){2,})",  re.MULTILINE)
_NUMBERED_BLOCK  = re.compile(r"((?:^[ \t]*\d+[.)]\s+.+\n?){2,})", re.MULTILINE)
_TABLE_BLOCK     = re.compile(r"(\|.+\|\n\|[-:| ]+\|\n(?:\|.+\|\n?)+)", re.MULTILINE)
_LIST_ITEM_STRIP = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])\s+",      re.MULTILINE)
_TABLE_PIPE      = re.compile(r"\|")


def _flatten_list(block: str) -> str:
    """Convert a multi-line bullet or numbered list into a flowing sentence."""
    lines = _LIST_ITEM_STRIP.sub("", block).strip().splitlines()
    items = [ln.rstrip(" ,;.") for ln in lines if ln.strip()]
    if not items:
        return block
    if len(items) == 1:
        return items[0] + "."
    return "; ".join(items[:-1]) + "; and " + items[-1] + "."


def _flatten_table(block: str) -> str:
    """Convert a Markdown table into one sentence per row."""
    rows = [r.strip() for r in block.strip().splitlines()]
    if len(rows) < 3:
        return block
    headers = [c.strip() for c in _TABLE_PIPE.split(rows[0]) if c.strip()]
    sentences = []
    for row in rows[2:]:
        cells = [c.strip() for c in _TABLE_PIPE.split(row) if c.strip()]
        parts = [f"{h}: {c}" for h, c in zip(headers, cells) if c]
        if parts:
            sentences.append(", ".join(parts) + ".")
    return " ".join(sentences) if sentences else block


# ─────────────────────────────────────────────────────────────────────────────
# Pass 3: Brand Nullification
# ─────────────────────────────────────────────────────────────────────────────
# Lore-friendly substitutions for well-known categories.
# Falls back to "[???]" for terms not in this map.

_LORE_SUBSTITUTIONS: dict[str, str] = {
    "starbucks":    "the Amber Leaf Trading Company",
    "mcdonald":     "the Golden Arch Tavern",
    "mcdonalds":    "the Golden Arch Tavern",
    "coca-cola":    "a fizzing alchemist's tonic",
    "coca cola":    "a fizzing alchemist's tonic",
    "coke":         "a fizzing alchemist's tonic",
    "pepsi":        "a rival alchemist's brew",
    "pepsi-cola":   "a rival alchemist's brew",
    "iphone":       "the crystalline communication slate",
    "android":      "the mechanical messenger",
    "google":       "the Omniscient Oracle",
    "amazon":       "the Eastern Trade Consortium",
    "microsoft":    "the Gatekeeper Guild",
    "facebook":     "the Social Registry",
    "twitter":      "the Message Board",
    "instagram":    "the Portrait Gallery",
    "netflix":      "the Royal Storyteller's Guild",
    "youtube":      "the Travelling Bard Network",
    "discord":      "the Echo Chamber",
    "spotify":      "the Enchanted Music Box",
    "reddit":       "the Town Crier's Board",
    "wikipedia":    "the Grand Lore Repository",
}


def _build_brand_patterns(
    blocklist: Sequence[str],
    extra: Sequence[str] | None = None,
) -> list[tuple[re.Pattern[str], str]]:
    """
    Compile brand patterns with their substitution strings.
    Longer terms come first to prevent short-term partial-match shadowing.
    """
    all_terms = list(blocklist) + list(extra or [])
    all_terms.sort(key=len, reverse=True)
    patterns: list[tuple[re.Pattern[str], str]] = []
    for term in all_terms:
        sub = _LORE_SUBSTITUTIONS.get(term.lower(), "[???]")
        patterns.append((
            re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE),
            sub,
        ))
    return patterns


# ─────────────────────────────────────────────────────────────────────────────
# ImmersionFilter
# ─────────────────────────────────────────────────────────────────────────────

class ImmersionFilter:
    """
    Post-generation scrubber and character-sheet UI-gate controller.

    All public methods are synchronous and stateless — the single shared
    instance can be called safely from concurrent async pipeline calls.

    Parameters
    ----------
    extra_blocklist:
        Additional terms to block beyond the seed BRAND_BLOCKLIST.
        Typically loaded from ``campaign_immersion_settings.custom_blocklist``.
    """

    def __init__(self, extra_blocklist: Sequence[str] | None = None) -> None:
        self._brand_patterns = _build_brand_patterns(BRAND_BLOCKLIST, extra_blocklist)

    # ── Public API ────────────────────────────────────────────────────────────

    def scrub_narrative(self, text: str) -> tuple[str, FilterReport]:
        """
        Apply all three immersion passes to the synthesised narrative.

        Returns (scrubbed_text, FilterReport).  The report carries per-pass
        replacement counts for telemetry and audit logging.
        """
        text, censor_n  = self._revert_censorship(text)
        text, flatten_n = self._flatten_lists(text)
        text, brand_n   = self._filter_brands(text)
        return text, FilterReport(
            censorship_reversions=censor_n,
            list_flattenings=flatten_n,
            brand_nullifications=brand_n,
        )

    def compute_state_hash(self, state: dict) -> str:
        """
        SHA-256 hash of the canonical JSON serialisation of a state dict.

        Keys are sorted so insertion order never produces a false positive.
        Non-serialisable values are coerced via ``default=str``.
        """
        canonical = json.dumps(
            state, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def should_render_character_sheet(
        self, pre_state: dict, post_state: dict
    ) -> bool:
        """
        Return True only when post_state differs from pre_state (TDR §3C).

        The Discord bot uses this flag to suppress the character-sheet embed
        on turns where no stat or inventory value changed, saving screen
        real-estate and reducing unnecessary API calls.
        """
        return self.compute_state_hash(pre_state) != self.compute_state_hash(post_state)

    # ── Pass 1 ─────────────────────────────────────────────────────────────────

    def _revert_censorship(self, text: str) -> tuple[str, int]:
        total = 0
        for pattern, replacement in _CENSORED_WORD_MAP:
            text, n = pattern.subn(replacement, text)
            total += n
        text, n = _INTRAWORD_ASTERISK.subn("", text)
        total += n
        if total:
            logger.debug("ImmersionFilter: %d censorship reversal(s) applied", total)
        return text, total

    # ── Pass 2 ─────────────────────────────────────────────────────────────────

    def _flatten_lists(self, text: str) -> tuple[str, int]:
        total = 0

        def _sub(fn, m: re.Match[str]) -> str:
            nonlocal total
            total += 1
            return fn(m.group(0))

        text = _BULLET_BLOCK.sub(lambda m: _sub(_flatten_list, m),  text)
        text = _NUMBERED_BLOCK.sub(lambda m: _sub(_flatten_list, m), text)
        text = _TABLE_BLOCK.sub(lambda m: _sub(_flatten_table, m),   text)
        if total:
            logger.debug("ImmersionFilter: %d markdown block(s) flattened", total)
        return text, total

    # ── Pass 3 ─────────────────────────────────────────────────────────────────

    def _filter_brands(self, text: str) -> tuple[str, int]:
        total = 0
        for pattern, sub in self._brand_patterns:
            text, n = pattern.subn(sub, text)
            total += n
        if total:
            logger.warning(
                "ImmersionFilter: %d brand term(s) nullified in final narrative", total
            )
        return text, total

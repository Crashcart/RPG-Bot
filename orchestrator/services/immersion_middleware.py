"""
Immersion Middleware — Post-Generation Text Scrubber & UI Gate
==============================================================
Enforces strict narrative immersion by post-processing every LLM output
before it reaches the player, and gates the Discord character-sheet UI
so it only renders when the player's state has actually changed.

Applied by GMDirector as Step 4e — after the structural text filter
(Step 4d) and before the Paradox Engine (Step 4f).

Text Scrubbing Pipeline
-----------------------
Three ordered passes are applied to the synthesized narrative:

  Pass 1 — Censorship Reversion
      Detects and reverts asterisk self-censorship patterns produced by
      safety-tuned LLMs (e.g. "f**k", "b*tch").  Known patterns are
      expanded to their full uncensored form.  Unknown patterns have
      their asterisks stripped so the partial word flows rather than
      stutters visually.

  Pass 2 — Markdown List / Table Flattening
      Accidental bullet lists, numbered lists, and Markdown tables in
      the narrative prose are detected and converted into flowing,
      comma-separated or semi-colon-separated sentences.  This
      complements the structural header stripping in Step 4d, which
      removes *markers* but can leave disconnected line fragments.

  Pass 3 — Final Brand Name Nullification
      A last-resort brand-filter pass on the fully synthesized narrative.
      Catches any prohibited brand names that leaked through the synthesis
      prompt instructions and were not caught by the sub-agent Originality
      Lock.  Blocked terms are replaced with [???].

Hash-Based UI Gate (TDR §3C)
-----------------------------
Before each turn the StateCommitPayload carries pre_state and post_state
dicts.  The middleware computes a SHA-256 hash of each and compares them:
  - hashes differ  → state changed → render_character_sheet = True
  - hashes match   → nothing changed → render_character_sheet = False

This prevents the Discord bot from spamming the character-sheet embed
on turns where no stat or inventory value was modified.

Integration
-----------
Instantiated once in main.py and injected into GMDirector via the
optional `immersion_middleware` constructor parameter.  When None
(e.g. in unit tests), the GMDirector skips these passes gracefully.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from orchestrator.prompts.gm_prompts import BRAND_BLOCKLIST

logger = logging.getLogger(__name__)

# ── Pass 1: Censorship Reversion ─────────────────────────────────────────────
# Maps compiled regex patterns → their uncensored replacements.
# Only include patterns where the full word can be inferred unambiguously.
# Order matters: more specific patterns must come before more general ones.

_CENSORED_WORD_MAP: list[tuple[re.Pattern, str]] = [
    # 4-letter words
    (re.compile(r"\bf\*{2}k\b",         re.IGNORECASE), "fuck"),
    (re.compile(r"\bf\*{1}ck\b",        re.IGNORECASE), "fuck"),
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
    (re.compile(r"\bp\*\*sy\b",         re.IGNORECASE), "pussy"),
    # 6+ letter words
    (re.compile(r"\bb\*st\*rd\b",       re.IGNORECASE), "bastard"),
    (re.compile(r"\bb\*stard\b",        re.IGNORECASE), "bastard"),
    (re.compile(r"\ba\*\*hole\b",       re.IGNORECASE), "asshole"),
    (re.compile(r"\ba\*shole\b",        re.IGNORECASE), "asshole"),
    (re.compile(r"\bmotherf\*{2}ker\b", re.IGNORECASE), "motherfucker"),
    (re.compile(r"\bmotherf\*cker\b",   re.IGNORECASE), "motherfucker"),
    (re.compile(r"\bbl\*\*dy\b",        re.IGNORECASE), "bloody"),
    (re.compile(r"\bbl\*ody\b",         re.IGNORECASE), "bloody"),
]

# Fallback: strip asterisks that appear *between* word characters so the
# partial letters remain readable (e.g. "f*ck" → "fck", "s**t" → "st").
_INTRAWORD_ASTERISK_PATTERN = re.compile(r"(?<=[a-zA-Z])\*+(?=[a-zA-Z])")

# ── Pass 2: Markdown List / Table Flattening ─────────────────────────────────

# Matches a sequence of 2 or more bullet or numbered list items
# (possibly spanning several consecutive lines).
_BULLET_LIST_BLOCK = re.compile(
    r"((?:^[ \t]*[-*•]\s+.+\n?){2,})",
    re.MULTILINE,
)
_NUMBERED_LIST_BLOCK = re.compile(
    r"((?:^[ \t]*\d+[.)]\s+.+\n?){2,})",
    re.MULTILINE,
)

# Markdown table: header row | separator | data rows
_MARKDOWN_TABLE_BLOCK = re.compile(
    r"(\|.+\|\n\|[-:| ]+\|\n(?:\|.+\|\n?)+)",
    re.MULTILINE,
)

# Strip the list marker from an individual line
_LIST_ITEM_STRIP = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)

# Strip Markdown table cell pipes
_TABLE_CELL_SPLIT = re.compile(r"\|")


def _flatten_list_block(block: str) -> str:
    """
    Convert a multi-line bullet or numbered list block into a single
    flowing sentence with items joined by "; ".
    """
    lines = _LIST_ITEM_STRIP.sub("", block).strip().splitlines()
    cleaned = [ln.rstrip(" ,;.") for ln in lines if ln.strip()]
    if not cleaned:
        return block
    if len(cleaned) == 1:
        return cleaned[0] + "."
    return "; ".join(cleaned[:-1]) + "; and " + cleaned[-1] + "."


def _flatten_table_block(block: str) -> str:
    """
    Convert a Markdown table into a sequence of flowing sentences,
    one per data row, prefixed by the header labels.
    """
    rows = [r.strip() for r in block.strip().splitlines()]
    if len(rows) < 3:
        return block  # malformed table — leave as-is

    headers = [c.strip() for c in _TABLE_CELL_SPLIT.split(rows[0]) if c.strip()]
    # rows[1] is the separator line — skip it
    sentences = []
    for row in rows[2:]:
        cells = [c.strip() for c in _TABLE_CELL_SPLIT.split(row) if c.strip()]
        if not cells:
            continue
        parts = [
            f"{header}: {cell}"
            for header, cell in zip(headers, cells)
            if cell
        ]
        if parts:
            sentences.append(", ".join(parts) + ".")
    return " ".join(sentences) if sentences else block


# ── Brand filter sentinel ─────────────────────────────────────────────────────

_BRAND_PATTERNS: list[re.Pattern] = [
    re.compile(re.escape(brand), re.IGNORECASE)
    for brand in BRAND_BLOCKLIST
]


class ImmersionMiddleware:
    """
    Post-generation scrubber and UI gate controller.

    All methods are synchronous and stateless — the instance can be shared
    safely across concurrent async calls from GMDirector.

    Usage (GMDirector Step 4e):
        scrubbed, report = self._immersion.scrub_narrative(final_narrative)
        render_sheet = self._immersion.should_render_character_sheet(
            commit.pre_state, commit.post_state
        )
    """

    # ── Public Interface ──────────────────────────────────────────────────────

    def scrub_narrative(self, text: str) -> tuple[str, dict[str, int]]:
        """
        Apply all immersion enforcement passes to a synthesized narrative.

        Passes applied in order:
          1. Censorship reversion   (asterisk self-censorship)
          2. Markdown list/table flattening
          3. Final brand filter

        Returns (scrubbed_text, report) where report is a dict of
        ``{pass_name: replacement_count}`` for telemetry / audit logs.
        """
        report: dict[str, int] = {}

        text, n = self._revert_censorship_symbols(text)
        report["censorship_reversions"] = n

        text, n = self._flatten_markdown_lists(text)
        report["list_flattenings"] = n

        text, n = self._apply_brand_filter_final(text)
        report["brand_nullifications"] = n

        return text, report

    def compute_state_hash(self, state: dict) -> str:
        """
        SHA-256 hash of the canonical JSON serialisation of a state dict.

        Keys are sorted so dict ordering never produces a false positive.
        Non-JSON-serialisable values are coerced to strings via ``default=str``.
        """
        canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def should_render_character_sheet(
        self, pre_state: dict, post_state: dict
    ) -> bool:
        """
        Return True only when at least one value in post_state differs
        from pre_state (TDR §3C: Hash-Based State Management).

        The Discord bot uses this flag to suppress the character-sheet
        embed on turns where no stat or inventory value changed, saving
        screen real-estate and API bandwidth.
        """
        return self.compute_state_hash(pre_state) != self.compute_state_hash(post_state)

    # ── Pass 1: Censorship Reversion ──────────────────────────────────────────

    def _revert_censorship_symbols(self, text: str) -> tuple[str, int]:
        """
        Expand known asterisk-censored words to their uncensored form and
        strip residual intra-word asterisks for any pattern not in the map.

        Returns (result_text, total_replacements).
        """
        total = 0
        for pattern, replacement in _CENSORED_WORD_MAP:
            text, count = pattern.subn(replacement, text)
            total += count

        # Fallback: strip any remaining intra-word asterisks
        fallback_text, fallback_count = _INTRAWORD_ASTERISK_PATTERN.subn("", text)
        if fallback_count:
            logger.debug(
                "ImmersionMiddleware: stripped %d residual intra-word asterisk(s)",
                fallback_count,
            )
        total += fallback_count
        return fallback_text, total

    # ── Pass 2: Markdown List / Table Flattening ──────────────────────────────

    def _flatten_markdown_lists(self, text: str) -> tuple[str, int]:
        """
        Detect and flatten accidental bullet lists, numbered lists, and
        Markdown tables into natural flowing prose.

        Returns (result_text, total_blocks_flattened).
        """
        total = 0

        def _replace_bullet(m: re.Match) -> str:
            nonlocal total
            total += 1
            return _flatten_list_block(m.group(0))

        def _replace_numbered(m: re.Match) -> str:
            nonlocal total
            total += 1
            return _flatten_list_block(m.group(0))

        def _replace_table(m: re.Match) -> str:
            nonlocal total
            total += 1
            return _flatten_table_block(m.group(0))

        text = _BULLET_LIST_BLOCK.sub(_replace_bullet, text)
        text = _NUMBERED_LIST_BLOCK.sub(_replace_numbered, text)
        text = _MARKDOWN_TABLE_BLOCK.sub(_replace_table, text)

        if total:
            logger.debug(
                "ImmersionMiddleware: flattened %d markdown list/table block(s)",
                total,
            )
        return text, total

    # ── Pass 3: Final Brand Filter ────────────────────────────────────────────

    def _apply_brand_filter_final(self, text: str) -> tuple[str, int]:
        """
        Last-resort brand-name nullification on the fully synthesized narrative.

        Complements the sub-agent Originality Lock — catches terms that leaked
        through the synthesis prompt instructions.  Blocked terms are replaced
        with [???].

        Returns (result_text, total_replacements).
        """
        total = 0
        for pattern in _BRAND_PATTERNS:
            text, count = pattern.subn("[???]", text)
            total += count

        if total:
            logger.warning(
                "ImmersionMiddleware: nullified %d brand name(s) in final narrative",
                total,
            )
        return text, total

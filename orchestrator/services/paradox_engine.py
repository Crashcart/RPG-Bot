"""
Paradox Engine — Unreliable Narrator Injection
===============================================
Post-processes the final narrative prose to introduce "unreliable narrator"
artefacts whose intensity scales with a per-campaign `paradox_level` (1–10).

Paradox levels
--------------
1–3  Subtle      Minor perceptual uncertainties; the narrator sounds slightly
                  unsure. Players may not notice.
4–6  Moderate    Contradictions appear within the same paragraph; sensory
                  details shift mid-sentence; the narrator corrects itself.
7–9  Heavy       Temporal glitches, fourth-wall cracks ("wait, that's not
                  right…"), looping phrases, reality audits.
10   Maximum     Full narrator breakdown.  The text stutters, rewrites itself,
                  and the GM's voice bleeds through the prose.

Integration
-----------
Called by GMDirector._apply_paradox() after the structural text filter (Step 4d)
and before the NarrativeResponsePayload is returned.  The paradox_level is read
from RealityWall's SQLite store so it persists across sessions.

When paradox_level == 1 (default), the engine returns the narrative unchanged —
zero overhead.
"""

from __future__ import annotations

import logging
import random
import re

logger = logging.getLogger(__name__)

# ── Injection templates by tier ───────────────────────────────────────────────

_SUBTLE_INSERTS = [
    " — or so it seems —",
    " (you think)",
    ", perhaps",
    " — at least, that's how it appears",
    ", if your eyes can be trusted",
]

_MODERATE_CORRECTIONS = [
    " Wait. No — ",
    " That is — actually — ",
    " (the narrator pauses) ",
    " …or was it ",
    " — strike that — ",
]

_HEAVY_GLITCHES = [
    "\n\n[REALITY AUDIT — discrepancy logged]\n\n",
    "\n\n…(the scene stutters)…\n\n",
    "\n\n— loop detected — resuming —\n\n",
    "\n\n[NARRATOR: this didn't happen yet]\n\n",
    "\n\n…wait. You've been here before.\n\n",
]

_MAX_BREAKDOWN_PREFIX = (
    "—[signal degraded]— "
)
_MAX_BREAKDOWN_SUFFIX = (
    "\n\n…[transmission ends]…\n\n"
    "…[resuming from last stable checkpoint]…"
)


class ParadoxEngine:
    """
    Stateless post-processor.  All state (paradox_level) lives in RealityWall.

    Usage:
        engine = ParadoxEngine()
        final  = engine.apply(narrative, paradox_level)
    """

    def apply(self, narrative: str, paradox_level: int) -> str:
        """
        Inject unreliable-narrator artefacts scaled to paradox_level.

        Returns the narrative unchanged when paradox_level == 1.
        """
        level = max(1, min(10, paradox_level))

        if level == 1:
            return narrative

        if level <= 3:
            return self._apply_subtle(narrative, intensity=level)
        if level <= 6:
            return self._apply_moderate(narrative, intensity=level - 3)
        if level <= 9:
            return self._apply_heavy(narrative, intensity=level - 6)
        return self._apply_maximum(narrative)

    # ── Tiers ─────────────────────────────────────────────────────────────────

    def _apply_subtle(self, text: str, intensity: int) -> str:
        """Insert uncertainty hedges after 1–2 random sentences."""
        sentences = _split_sentences(text)
        if len(sentences) < 2:
            return text

        n_inserts = intensity  # 1 or 2
        indices   = random.sample(range(len(sentences)), min(n_inserts, len(sentences)))
        for idx in sorted(indices, reverse=True):
            hedge = random.choice(_SUBTLE_INSERTS)
            # Insert before the sentence-ending punctuation
            sentences[idx] = re.sub(r'([.!?])\s*$', hedge + r'\1', sentences[idx])

        result = " ".join(sentences)
        logger.debug("ParadoxEngine: subtle injection (level=%d)", intensity)
        return result

    def _apply_moderate(self, text: str, intensity: int) -> str:
        """Splice self-corrections into the middle of the narrative."""
        sentences = _split_sentences(text)
        if len(sentences) < 3:
            return self._apply_subtle(text, intensity)

        n_splices = intensity  # 1–3
        mid = len(sentences) // 2
        for i in range(n_splices):
            idx = max(0, mid - i)
            if idx < len(sentences):
                correction = random.choice(_MODERATE_CORRECTIONS)
                # Append correction fragment + repeat next sentence start
                tail = sentences[idx].split()[0] if sentences[idx].split() else "it"
                sentences[idx] = sentences[idx].rstrip(".!?") + correction + tail + "…"

        result = " ".join(sentences)
        logger.debug("ParadoxEngine: moderate injection (level=%d)", intensity + 3)
        return result

    def _apply_heavy(self, text: str, intensity: int) -> str:
        """Insert reality glitch blocks between paragraphs."""
        paragraphs = text.split("\n\n") if "\n\n" in text else [text]
        if len(paragraphs) == 1:
            # No paragraph breaks — split at midpoint
            mid = len(text) // 2
            paragraphs = [text[:mid], text[mid:]]

        n_glitches = intensity  # 1–3
        insert_points = random.sample(
            range(1, len(paragraphs) + 1),
            min(n_glitches, len(paragraphs)),
        )
        glitch_blocks = random.choices(_HEAVY_GLITCHES, k=n_glitches)

        for offset, (point, glitch) in enumerate(
            zip(sorted(insert_points), glitch_blocks)
        ):
            paragraphs.insert(point + offset, glitch)

        result = "\n\n".join(paragraphs)
        logger.debug("ParadoxEngine: heavy injection (level=%d)", intensity + 6)
        return result

    def _apply_maximum(self, text: str) -> str:
        """Full narrator breakdown: prefix, stutter the first line, suffix."""
        first_line = text.split(".")[0] if "." in text else text[:80]
        # Stutter the first sentence
        words  = first_line.split()
        if len(words) > 3:
            mid    = len(words) // 2
            stutter = " ".join(words[:mid]) + "… " + " ".join(words[:mid]) + "… " + " ".join(words)
        else:
            stutter = first_line

        rest = text[len(first_line):].lstrip(". ")
        result = (
            _MAX_BREAKDOWN_PREFIX
            + stutter + ". "
            + rest
            + _MAX_BREAKDOWN_SUFFIX
        )
        logger.debug("ParadoxEngine: maximum breakdown (level=10)")
        return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    """Naively split text into sentences on . ! ? boundaries."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p for p in parts if p]

"""
Ironclad GM – Speaker Diarizer
================================
Parses `[Speaker]: text` tags from GM synthesis output, resolves each
speaker to a Piper voice model, persists new NPC voice profiles to the
database, and returns an ordered list of TTSCue objects ready for voice
channel delivery.

Speaker Tag Format
------------------
The GM synthesis pass emits tags when speaker_tags_enabled is True:

    [Narrator]: The tavern is dark and smoky.  Grib eyes you warily.
    [NPC_Grib]: "Who goes there?!"
    [Narrator]: His hand drifts toward the cudgel behind the bar.

Rules:
  • [Narrator] → always maps to the configured narrator Piper voice model.
  • [NPC_<Name>] → maps to the NPC's persisted voice profile.  On first
    encounter a voice is auto-assigned from available Piper models.
  • Text that has no speaker tags is treated as Narrator prose.
  • Tags are stripped from the cleaned display narrative.

Integration
-----------
The diarizer runs as a post-process step inside GMDirector.narrate() AFTER
the synthesis pass, only when tts_provider == "piper" and the output
contains at least one speaker tag.  The cleaned narrative is written to
NarrativeResponsePayload.narrative; TTSCues replace the existing cue list.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.services.piper_client import PiperClient

from orchestrator.schemas.payloads import TTSCue

logger = logging.getLogger(__name__)

# Matches [Narrator], [NPC_Grib], [NPC_Barkeep], etc.
# Group 1 = raw speaker label (e.g. "Narrator", "NPC_Grib")
_SPEAKER_TAG_RE = re.compile(r"\[([^\]]+)\]:\s*")

# Prefix used by the GM for NPC speakers
_NPC_PREFIX = "NPC_"

# Fallback pool of distinct Piper voices for auto-assigning new NPCs.
# The diarizer cycles through this list in a deterministic rotation so
# NPCs encountered early in a campaign always get different voices.
_DEFAULT_NPC_VOICE_POOL = [
    "en_US-ryan-high",
    "en_GB-alan-medium",
    "en_US-joe-medium",
    "en_US-kathleen-low",
    "en_GB-jenny_dioco-medium",
    "en_US-arctic-medium",
    "en_US-norman-medium",
    "en_US-danny-low",
]


def has_speaker_tags(text: str) -> bool:
    """Return True if the text contains at least one [Speaker]: tag."""
    return bool(_SPEAKER_TAG_RE.search(text))


def strip_speaker_tags(text: str) -> str:
    """
    Remove all [Speaker]: prefixes from a tagged text block.

    The dialogue content is preserved; only the structural tags are removed.
    Multiple blank lines collapsed to a single blank line.
    """
    cleaned = _SPEAKER_TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def parse_speaker_segments(text: str) -> list[tuple[str, str]]:
    """
    Split a speaker-tagged text into (speaker_label, text_chunk) pairs.

    If the text contains no tags the entire text is attributed to "Narrator".

    Example
    -------
    Input:
        "[Narrator]: The door creaks open.\n[NPC_Grib]: Who are you?!"
    Output:
        [("Narrator", "The door creaks open."),
         ("NPC_Grib", "Who are you?!")]
    """
    if not has_speaker_tags(text):
        return [("Narrator", text.strip())] if text.strip() else []

    segments: list[tuple[str, str]] = []
    parts = _SPEAKER_TAG_RE.split(text)
    # split() with a capturing group gives: ['pre', 'speaker1', 'text1', 'speaker2', 'text2', ...]
    # parts[0] is any pre-tag text (usually empty); odd-indexed = speaker, even-indexed (>0) = text
    # If text before the first tag is non-empty, attribute it to Narrator
    if parts[0].strip():
        segments.append(("Narrator", parts[0].strip()))

    for i in range(1, len(parts) - 1, 2):
        speaker = parts[i].strip()
        chunk   = parts[i + 1].strip() if (i + 1) < len(parts) else ""
        if chunk:
            segments.append((speaker, chunk))

    return segments


class SpeakerDiarizer:
    """
    Resolves GM speaker tags to Piper voice profiles and builds TTSCues.

    The diarizer is stateless per-call but caches profile lookups within
    a single narrate() turn to avoid repeated DB queries for the same NPC.

    Voice assignment order for new NPCs:
      1. Look up npc_voice_profiles in PostgreSQL — use persisted voice.
      2. Not found → pick next unused voice from _DEFAULT_NPC_VOICE_POOL
         (rotation index derived from total existing profiles for this
         campaign to ensure variety even after restarts).
      3. Persist the new assignment so the same NPC sounds the same next
         session.
    """

    def __init__(self, piper_client: "PiperClient", db=None) -> None:
        self._piper  = piper_client
        self._db     = db

    # ── Public API ─────────────────────────────────────────────────────────────

    async def diarize(
        self,
        raw_narrative:   str,
        campaign_id:     str,
    ) -> tuple[str, list[TTSCue]]:
        """
        Process a speaker-tagged narrative.

        Parameters
        ----------
        raw_narrative : Full GM synthesis output, potentially with [Speaker]: tags.
        campaign_id   : Active campaign UUID (for voice profile persistence).

        Returns
        -------
        (clean_narrative, tts_cues)
          clean_narrative — narrative text with all speaker tags stripped.
          tts_cues        — ordered TTSCue list for voice channel delivery.
                            Empty list when no tags found or Piper is disabled.
        """
        if not self._piper.enabled:
            return raw_narrative, []

        if not has_speaker_tags(raw_narrative):
            # No tagging — emit a single Narrator cue for the full text
            narrator_cue = TTSCue(
                entity_name="Narrator",
                text=raw_narrative.strip(),
                voice_id=self._piper.default_narrator_model,
                node_name="piper",
            )
            return raw_narrative, [narrator_cue]

        segments      = parse_speaker_segments(raw_narrative)
        clean_text    = strip_speaker_tags(raw_narrative)
        tts_cues: list[TTSCue] = []

        # Per-call cache to avoid redundant DB round-trips for the same NPC
        _profile_cache: dict[str, str] = {}

        for speaker_label, chunk in segments:
            voice_id = await self._resolve_voice(
                speaker_label, campaign_id, _profile_cache
            )
            tts_cues.append(TTSCue(
                entity_name=speaker_label.removeprefix(_NPC_PREFIX),
                text=chunk,
                voice_id=voice_id,
                node_name="piper",
            ))

        return clean_text, tts_cues

    # ── Voice Resolution ───────────────────────────────────────────────────────

    async def _resolve_voice(
        self,
        speaker_label:  str,
        campaign_id:    str,
        profile_cache:  dict[str, str],
    ) -> str:
        """
        Return the Piper voice model ID for a given speaker label.

        For "Narrator" this is always the configured narrator model.
        For NPC_* labels the voice is looked up or auto-assigned from the DB.
        """
        if speaker_label == "Narrator":
            return self._piper.default_narrator_model

        if speaker_label in profile_cache:
            return profile_cache[speaker_label]

        npc_name = speaker_label.removeprefix(_NPC_PREFIX).lower()
        voice_id = await self._get_or_create_npc_voice(npc_name, campaign_id)
        profile_cache[speaker_label] = voice_id
        return voice_id

    async def _get_or_create_npc_voice(
        self,
        npc_name:    str,
        campaign_id: str,
    ) -> str:
        """
        Fetch existing NPC voice profile from DB, or create and persist a new one.

        Falls back to the default NPC model when the DB is unavailable.
        """
        if self._db is None:
            return self._piper.default_npc_model

        try:
            row = await self._db.pool.fetchrow(
                """
                SELECT voice_model_id FROM npc_voice_profiles
                WHERE campaign_id = $1 AND npc_name = $2
                """,
                campaign_id,
                npc_name,
            )
            if row:
                return row["voice_model_id"]

            # Auto-assign next voice from pool
            voice_id = await self._pick_next_voice(campaign_id)
            await self._db.pool.execute(
                """
                INSERT INTO npc_voice_profiles
                    (campaign_id, npc_name, voice_model_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (campaign_id, npc_name) DO NOTHING
                """,
                campaign_id,
                npc_name,
                voice_id,
            )
            logger.info(
                "SpeakerDiarizer: assigned voice '%s' to NPC '%s' in campaign %s",
                voice_id, npc_name, campaign_id,
            )
            return voice_id

        except Exception as exc:
            logger.warning(
                "SpeakerDiarizer: DB voice lookup failed for NPC '%s': %s — using default",
                npc_name, exc,
            )
            return self._piper.default_npc_model

    async def _pick_next_voice(self, campaign_id: str) -> str:
        """
        Pick the next voice from the pool in a round-robin fashion based on
        how many NPC profiles already exist for this campaign.

        Prefers voices available on the Piper service; falls back to pool
        entries if the service is unreachable (models may not be installed).
        """
        pool = _DEFAULT_NPC_VOICE_POOL

        # Optionally filter to actually available models
        available = await self._piper.list_voices()
        if available:
            npc_pool = [v for v in pool if v in available]
            if npc_pool:
                pool = npc_pool

        try:
            count = await self._db.pool.fetchval(
                "SELECT COUNT(*) FROM npc_voice_profiles WHERE campaign_id = $1",
                campaign_id,
            )
            idx = int(count or 0) % len(pool)
        except Exception:
            idx = 0

        return pool[idx]

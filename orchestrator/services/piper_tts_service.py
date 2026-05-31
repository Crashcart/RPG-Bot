"""
PiperTTSService — Local neural TTS via lscr.io/linuxserver/piper.

Pipeline:
  1. Parse speaker-tagged narrative text into segments ([Narrator] / [NPC Name])
  2. Resolve per-NPC voice model from PostgreSQL (Redis-cached 24 h)
  3. POST text to the Piper HTTP API and return WAV bytes
  4. Cache synthesised WAV in Redis (TTL 86400 s) for identical text+voice pairs
  5. Build TTSCue-compatible dicts for NarrativeResponsePayload.tts_cues

Speaker tag format expected from GMDirector prompts:
  [Narrator]: The tavern falls silent.
  [Grib the Goblin]: \"Who goes there?!\"

Falls back to a single Narrator segment when the narrative contains no tags.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Sentence boundary — used for low-latency chunked streaming
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

# Speaker tag: [Speaker Name]: text content (non-greedy, multiline)
_SPEAKER_TAG = re.compile(
    r'\[([^\]]+)\]:\s*((?:(?!\[[^\]]+\]:).)+)',
    re.DOTALL,
)

# Default narrator Piper voice model (en_US-lessac-medium ~63 MB)
NARRATOR_VOICE: str = "en_US-lessac-medium"

# Speaker aliases treated as the narrator
_NARRATOR_ALIASES: frozenset[str] = frozenset(
    {"narrator", "gm", "game master", "dm", "dungeon master"}
)


@dataclass
class SpeakerSegment:
    """A single tagged block of narrative text from one speaker."""

    speaker: str
    text: str
    is_narrator: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_narrator = self.speaker.lower() in _NARRATOR_ALIASES


class PiperTTSService:
    """
    Orchestrator service for local neural TTS backed by a Piper container.

    All synthesis failures are non-fatal: methods return ``None`` / empty
    collections rather than raising so a broken TTS container never crashes
    the main pipeline turn.
    """

    def __init__(
        self,
        piper_url: str,
        redis: Any,
        db: Any,
        narrator_model: str = NARRATOR_VOICE,
    ) -> None:
        self._piper_url = piper_url.rstrip("/")
        self._redis = redis
        self._db = db
        self._narrator_model = narrator_model

    # ── Text parsing ──────────────────────────────────────────────────────────

    def parse_segments(self, text: str) -> list[SpeakerSegment]:
        """
        Extract speaker segments from tagged narrative text.

        Falls back to a single Narrator segment when no ``[Tag]:`` markup is
        found, preserving backward compatibility with un-tagged GMDirector output.
        """
        segments = [
            SpeakerSegment(speaker=m.group(1).strip(), text=m.group(2).strip())
            for m in _SPEAKER_TAG.finditer(text)
        ]
        if not segments:
            segments = [SpeakerSegment(speaker="Narrator", text=text.strip())]
        return segments

    def chunk_sentences(self, text: str) -> list[str]:
        """
        Split text on sentence-ending punctuation (.!?) for chunked synthesis.

        Piper generates audio in real time as tokens arrive.  Sending one
        sentence at a time lets the Discord bot begin playback while the GM
        Director is still generating the rest of the paragraph.
        """
        return [p.strip() for p in _SENTENCE_END.split(text.strip()) if p.strip()]

    # ── Voice model persistence ───────────────────────────────────────────────

    async def get_npc_voice(self, npc_name: str, campaign_id: str) -> str:
        """
        Return the Piper model ID for an NPC, cached 24 h in Redis.

        Falls back to the narrator model when the NPC has no assignment yet.
        """
        cache_key = f"tts:voice:{campaign_id}:{npc_name.lower()}"
        cached = await self._redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else str(cached)

        try:
            row = await self._db.pool.fetchrow(
                "SELECT voice_model_id FROM npc_voice_assignments "
                "WHERE npc_name_lower = $1 AND campaign_id = $2",
                npc_name.lower(),
                campaign_id,
            )
            model: str = row["voice_model_id"] if row else self._narrator_model
        except Exception:
            logger.debug(
                "npc_voice_assignments lookup failed for '%s', using narrator default",
                npc_name,
            )
            model = self._narrator_model

        await self._redis.setex(cache_key, 86400, model)
        return model

    async def set_npc_voice(
        self, npc_name: str, campaign_id: str, voice_model_id: str
    ) -> None:
        """
        Persist an NPC voice assignment and refresh the Redis cache.
        Idempotent — re-calling with the same NPC name updates the existing row.
        """
        await self._db.pool.execute(
            """
            INSERT INTO npc_voice_assignments (npc_name_lower, campaign_id, voice_model_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (npc_name_lower, campaign_id)
            DO UPDATE SET voice_model_id = EXCLUDED.voice_model_id,
                          updated_at     = now()
            """,
            npc_name.lower(),
            campaign_id,
            voice_model_id,
        )
        cache_key = f"tts:voice:{campaign_id}:{npc_name.lower()}"
        await self._redis.setex(cache_key, 86400, voice_model_id)

    # ── Synthesis ─────────────────────────────────────────────────────────────

    def _wav_cache_key(self, text: str, voice_model_id: str) -> str:
        digest = hashlib.sha256(
            f"{voice_model_id}\x00{text}".encode()
        ).hexdigest()[:24]
        return f"tts:wav:{digest}"

    async def synthesise(self, text: str, voice_model_id: str) -> bytes | None:
        """
        POST ``text`` to the Piper /api/tts endpoint and return WAV bytes.

        The linuxserver/piper container exposes:
            POST /api/tts?voice=<model_id>
            Content-Type: text/plain
            Body: plain text to synthesise
            Response: audio/wav

        Returns:
            WAV bytes on success, ``None`` on any network or HTTP error.

        The WAV is cached in Redis (24 h TTL) keyed on SHA-256(voice+text) so
        repeated identical lines (e.g. NPC catch-phrases) skip the Piper call.
        """
        if not text.strip():
            return None

        cache_key = self._wav_cache_key(text, voice_model_id)
        cached = await self._redis.get(cache_key)
        if cached:
            return cached if isinstance(cached, bytes) else cached.encode()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._piper_url}/api/tts",
                    params={"voice": voice_model_id},
                    content=text.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
                resp.raise_for_status()
                wav_bytes = resp.content

            await self._redis.setex(cache_key, 86400, wav_bytes)
            return wav_bytes

        except Exception as exc:
            logger.warning(
                "Piper TTS synthesis failed — voice=%s len=%d error=%s",
                voice_model_id,
                len(text),
                exc,
            )
            return None

    # ── High-level pipeline helper ────────────────────────────────────────────

    async def build_tts_cues(
        self,
        narrative_text: str,
        campaign_id: str,
    ) -> list[dict[str, str]]:
        """
        Parse narrative text, resolve per-speaker voices, return TTSCue dicts.

        Each dict is compatible with ``TTSCue`` in ``schemas/payloads.py``:
            entity_name, text, voice_id, node_name

        Segments whose synthesis is unavailable are still included in the
        returned list (with their resolved ``voice_id``) so the Discord bot
        can fall back to edge-tts if the Piper container is unreachable.
        """
        segments = self.parse_segments(narrative_text)
        cues: list[dict[str, str]] = []
        for seg in segments:
            if seg.is_narrator:
                voice = self._narrator_model
            else:
                voice = await self.get_npc_voice(seg.speaker, campaign_id)
            cues.append(
                {
                    "entity_name": seg.speaker,
                    "text": seg.text,
                    "voice_id": voice,
                    "node_name": "piper-tts",
                }
            )
        return cues

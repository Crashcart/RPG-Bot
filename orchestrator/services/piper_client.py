"""
Ironclad GM – Piper TTS Client
================================
Async HTTP client for the local Piper TTS service (piper-tts container on
port 5500).  Handles voice synthesis requests, voice listing, and graceful
degradation when the service is unavailable.

Piper is a zero-cost FOSS neural TTS engine optimised for CPU-only inference.
It generates studio-quality voices at real-time speed on standard hardware,
making it suitable for per-turn NPC voice synthesis without cloud API costs.

Usage
-----
    client = PiperClient(settings)
    audio_bytes = await client.synthesize(
        text="Who goes there?!",
        voice_model="en_US-ryan-high",
        speed_scale=1.1,
    )

Voice model files must be installed in the piper-tts container's /models
directory.  Download from:
  https://github.com/rhasspy/piper/releases/tag/2023.11.14-2

Each model requires two files: <name>.onnx and <name>.onnx.json
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from orchestrator.config import Settings

logger = logging.getLogger(__name__)

_SYNTHESIZE_TIMEOUT = 30.0   # seconds — CPU synthesis can be slow for long passages
_VOICES_TIMEOUT     = 5.0


class PiperClient:
    """
    Async client for the Piper TTS HTTP service.

    All methods return graceful fallback values (None / empty list) when the
    service is unreachable rather than raising — the orchestrator treats TTS
    as a best-effort enrichment, never a blocking dependency.
    """

    def __init__(self, settings: "Settings") -> None:
        self._base_url = settings.piper_url
        self._enabled  = bool(self._base_url)
        self._default_narrator_model = settings.piper_default_narrator_model
        self._default_npc_model      = settings.piper_default_npc_model
        self._client: httpx.AsyncClient | None = None
        if self._enabled:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=_SYNTHESIZE_TIMEOUT,
            )
            logger.info("PiperClient initialised: base_url=%s", self._base_url)
        else:
            logger.info("PiperClient disabled (PIPER_URL not set).")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def default_narrator_model(self) -> str:
        return self._default_narrator_model

    @property
    def default_npc_model(self) -> str:
        return self._default_npc_model

    async def synthesize(
        self,
        text:        str,
        voice_model: str | None = None,
        speed_scale: float      = 1.0,
        noise_scale: float      = 0.667,
        noise_w:     float      = 0.8,
    ) -> bytes | None:
        """
        Synthesize speech from text and return WAV audio bytes.

        Parameters
        ----------
        text        : The text to synthesize.
        voice_model : Piper model name (e.g. "en_US-lessac-medium").  Falls
                      back to PIPER_DEFAULT_NARRATOR_MODEL when None.
        speed_scale : Playback speed multiplier.  >1 = faster, <1 = slower.
        noise_scale : Phoneme duration variability (0.0–1.0).
        noise_w     : Pitch variability (0.0–1.0).

        Returns
        -------
        WAV audio bytes, or None when synthesis fails or the service is
        unavailable.
        """
        if not self._enabled or not self._client:
            return None

        model = voice_model or self._default_narrator_model
        try:
            resp = await self._client.post(
                "/api/tts",
                json={
                    "text":        text,
                    "voice_model": model,
                    "speed_scale": speed_scale,
                    "noise_scale": noise_scale,
                    "noise_w":     noise_w,
                },
            )
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "PiperClient.synthesize: HTTP %d for model=%s — %s",
                exc.response.status_code, model, exc.response.text[:200],
            )
        except Exception as exc:
            logger.warning("PiperClient.synthesize failed (model=%s): %s", model, exc)
        return None

    async def list_voices(self) -> list[str]:
        """
        Return the list of available Piper voice model names.

        Returns an empty list when the service is unavailable.
        """
        if not self._enabled or not self._client:
            return []
        try:
            resp = await self._client.get("/api/voices", timeout=_VOICES_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("PiperClient.list_voices failed: %s", exc)
            return []

    async def health_check(self) -> bool:
        """
        Returns True when the Piper service is reachable and healthy.
        Used by the System Integrity Check (SIC) pillar.
        """
        if not self._enabled or not self._client:
            return False
        try:
            resp = await self._client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        """Cleanly close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()

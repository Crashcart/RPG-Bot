"""
Ironclad GM – ElevenLabs Client
================================
Handles AI-generated sound effects (SFX) via the ElevenLabs Sound Generation
API, and optional TTS via the ElevenLabs Text-to-Speech API.

All generated assets are cached by SHA-256(text) in /app/assets/sfx/ and
served through the media-proxy so the Discord bot can access them by URL.

Graceful no-op when ELEVENLABS_API_KEY is not set.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

from orchestrator.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

_SFX_API_URL  = "https://api.elevenlabs.io/v1/sound-generation"
_TTS_API_URL  = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_ASSET_DIR    = Path("/app/assets/sfx")
_TTS_ASSET_DIR = Path("/app/assets/tts")


class ElevenLabsClient:
    """
    Client for ElevenLabs AI-powered audio generation.

    SFX: POST /v1/sound-generation — generates 1-22 s clips from text descriptions.
    TTS: POST /v1/text-to-speech/{voice_id} — generates NPC voice from text.

    Both methods return a media-proxy URL (e.g. http://media-asset-proxy:8001/sfx/{hash}.mp3)
    or None when the API key is not configured or generation fails.
    """

    def __init__(self) -> None:
        self._api_key  = settings.elevenlabs_api_key
        self._enabled  = bool(self._api_key)
        self._media_url = settings.media_proxy_url
        self._client: httpx.AsyncClient | None = None
        if self._enabled:
            self._client = httpx.AsyncClient(
                headers={
                    "xi-api-key":   self._api_key,
                    "Content-Type": "application/json",
                    "Accept":       "audio/mpeg",
                },
                timeout=30.0,
            )
        _ASSET_DIR.mkdir(parents=True, exist_ok=True)
        _TTS_ASSET_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # SFX generation
    # ------------------------------------------------------------------

    async def generate_sfx(
        self,
        text: str,
        duration_seconds: float | None = None,
        prompt_influence: float = 0.3,
    ) -> str | None:
        """
        Generate a one-shot sound effect from a text description.

        Args:
            text: Plain-English description, e.g. "heavy iron door slamming shut"
            duration_seconds: Target clip length (1–22 s). None = auto.
            prompt_influence: 0.0–1.0, how strictly the model follows the prompt.

        Returns:
            Media-proxy URL to the generated .mp3, or None on failure.
        """
        if not self._enabled:
            logger.debug("ElevenLabs SFX skipped — no API key configured")
            return None

        # Cache by content hash so identical SFX are never re-generated
        cache_key  = hashlib.sha256(text.encode()).hexdigest()[:24]
        cache_path = _ASSET_DIR / f"{cache_key}.mp3"
        if cache_path.exists():
            return f"{self._media_url}/sfx/{cache_key}.mp3"

        payload: dict = {
            "text":               text,
            "prompt_influence":   prompt_influence,
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds

        try:
            assert self._client is not None
            resp = await self._client.post(_SFX_API_URL, json=payload)
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
            logger.info("ElevenLabs SFX generated: %s (%d bytes)", text[:60], len(resp.content))
            return f"{self._media_url}/sfx/{cache_key}.mp3"
        except httpx.HTTPStatusError as exc:
            logger.error("ElevenLabs SFX HTTP error %s: %s", exc.response.status_code, text[:60])
            return None
        except Exception as exc:
            logger.error("ElevenLabs SFX error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # TTS generation (optional provider)
    # ------------------------------------------------------------------

    async def generate_tts(
        self,
        text: str,
        voice_id: str,
        model_id: str = "eleven_multilingual_v2",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
    ) -> str | None:
        """
        Generate NPC TTS speech via ElevenLabs.

        Args:
            text:            Dialogue to speak.
            voice_id:        ElevenLabs voice ID (not edge-tts voice name).
            model_id:        ElevenLabs model to use.
            stability:       Voice stability (0.0–1.0).
            similarity_boost: Voice similarity boost (0.0–1.0).

        Returns:
            Media-proxy URL to the generated .mp3, or None on failure.
        """
        if not self._enabled:
            return None

        cache_key  = hashlib.sha256(f"{voice_id}:{text}".encode()).hexdigest()[:24]
        cache_path = _TTS_ASSET_DIR / f"{cache_key}.mp3"
        if cache_path.exists():
            return f"{self._media_url}/tts/{cache_key}.mp3"

        url     = _TTS_API_URL.format(voice_id=voice_id)
        payload = {
            "text":       text,
            "model_id":   model_id,
            "voice_settings": {
                "stability":        stability,
                "similarity_boost": similarity_boost,
            },
        }
        try:
            assert self._client is not None
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
            return f"{self._media_url}/tts/{cache_key}.mp3"
        except httpx.HTTPStatusError as exc:
            logger.error("ElevenLabs TTS HTTP error %s for voice %s", exc.response.status_code, voice_id)
            return None
        except Exception as exc:
            logger.error("ElevenLabs TTS error: %s", exc)
            return None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

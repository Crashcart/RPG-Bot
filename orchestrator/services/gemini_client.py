"""Client for the Google Gemini narrative generation API."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path

import httpx

from orchestrator.config import Settings
from orchestrator.prompts.guardrails import build_narrative_system_prompt
from orchestrator.schemas.payloads import NarrativeRequestPayload, NarrativeResponsePayload

logger = logging.getLogger(__name__)

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Lyria 3 model IDs
_LYRIA_CLIP  = "lyria-3-clip-preview"   # 30-second clips
_LYRIA_PRO   = "lyria-3-pro-preview"    # up to 3 minutes

_MUSIC_ASSET_DIR = Path("/app/assets/music")


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self._settings  = settings
        self._api_key   = settings.gemini_api_key
        self._model     = settings.gemini_model
        self._node_name = "gemini-cloud"
        self._media_url = settings.media_proxy_url

    # ── Generic text generation (used by GMDirector) ─────────────────────────

    async def generate(
        self,
        system_prompt: str,
        user_prompt:   str,
        max_tokens:    int = 800,
    ) -> str:
        """
        Low-level free-form text generation via Gemini.

        Used by the GM Director for planning and synthesis passes when the
        Cloud Storyteller is active.  Mirrors the same interface as
        OllamaClient.generate() so the GMDirector can call either
        transparently.

        Args:
            system_prompt: Gemini system_instruction text.
            user_prompt:   User-turn content.
            max_tokens:    maxOutputTokens for the generation config.

        Returns:
            The generated text, stripped of leading/trailing whitespace.
        """
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature":     0.85,
                "maxOutputTokens": max_tokens,
                "topP":            0.95,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HARASSMENT",         "threshold": "BLOCK_NONE"},
            ],
        }
        url = f"{_GEMINI_API_BASE}/{self._model}:generateContent?key={self._api_key}"

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            logger.error("Unexpected Gemini response in generate(): %s", json.dumps(data)[:400])
            raise ValueError("Could not extract text from Gemini response.") from exc

    # ── Lyria 3 Music Generation ─────────────────────────────────────────────

    async def generate_music(
        self,
        music_prompt: str,
        scene_type:   str,
        duration:     str = "clip",
        db=None,
    ) -> str | None:
        """
        Generate ambient music using Gemini Lyria 3.

        The audio bytes returned by the API are saved to /app/assets/music/
        and served via the media-proxy.  Subsequent calls with the same prompt
        return the cached URL immediately (SHA-256 key).

        Args:
            music_prompt: Descriptive prose for Lyria (tempo, instruments, mood).
            scene_type:   Semantic label for logging (combat, exploration, etc.).
            duration:     "clip" → lyria-3-clip-preview (30 s)
                          "long" → lyria-3-pro-preview (up to 3 min)
            db:           Optional DatabaseService — if provided, reads 'music_model'
                          from system_settings.  Returns None immediately when model
                          is set to 'lavalink'.

        Returns:
            Media-proxy URL (str) or None on failure / when Lyria is disabled.
        """
        # Check runtime setting if DB is available
        if db is not None:
            music_model = await db.get_system_setting("music_model", "lyria-3-clip-preview")
            if music_model == "lavalink":
                logger.debug("generate_music: music_model=lavalink — skipping Lyria")
                return None
            lyria_model = _LYRIA_PRO if music_model == _LYRIA_PRO else _LYRIA_CLIP
        else:
            lyria_model = _LYRIA_PRO if duration == "long" else _LYRIA_CLIP

        # Cache by SHA-256 of prompt + model
        cache_key  = hashlib.sha256(f"{lyria_model}:{music_prompt}".encode()).hexdigest()[:24]
        _MUSIC_ASSET_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = _MUSIC_ASSET_DIR / f"{cache_key}.mp3"
        if cache_path.exists():
            logger.debug("Music cache hit: %s (%s)", scene_type, cache_key)
            return f"{self._media_url}/music/{cache_key}.mp3"

        payload = {
            "contents": [{"parts": [{"text": music_prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
            },
        }
        url = f"{_GEMINI_API_BASE}/{lyria_model}:generateContent?key={self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()

            data = response.json()
            # Audio is returned as base64-encoded data in inline_data
            parts = data["candidates"][0]["content"]["parts"]
            audio_b64 = None
            for part in parts:
                if "inlineData" in part:
                    audio_b64 = part["inlineData"]["data"]
                    break
                if "inline_data" in part:
                    audio_b64 = part["inline_data"]["data"]
                    break

            if not audio_b64:
                logger.error("Lyria returned no audio data for scene_type=%s", scene_type)
                return None

            audio_bytes = base64.b64decode(audio_b64)
            cache_path.write_bytes(audio_bytes)
            logger.info(
                "Lyria music generated: scene=%s model=%s size=%d bytes",
                scene_type, lyria_model, len(audio_bytes),
            )
            return f"{self._media_url}/music/{cache_key}.mp3"

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Lyria HTTP error %s for scene_type=%s: %s",
                exc.response.status_code, scene_type, exc.response.text[:200],
            )
            return None
        except Exception as exc:
            logger.error("Lyria generation error for scene_type=%s: %s", scene_type, exc)
            return None

    # ── Visual Intel — Image Analysis ────────────────────────────────────────

    async def generate_with_image(
        self,
        system_prompt: str,
        user_prompt:   str,
        image_url:     str,
        max_tokens:    int = 400,
    ) -> str:
        """
        Analyse an image URL using Gemini Vision.

        Downloads the image, base64-encodes it, and sends it alongside the
        text prompt.  Returns a text description/analysis of the image.

        Used by:
          • Discord Visual Intel: player attaches an image to /act
          • GM Sandbox: admin drags an image into the chat for GM analysis
        """
        async with httpx.AsyncClient(timeout=20) as client:
            img_resp = await client.get(image_url)
            img_resp.raise_for_status()
        image_bytes   = img_resp.content
        image_b64     = base64.b64encode(image_bytes).decode()
        # Detect MIME type from Content-Type header or URL extension
        content_type  = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": content_type, "data": image_b64}},
                    {"text": user_prompt},
                ]
            }],
            "generationConfig": {
                "temperature":     0.6,
                "maxOutputTokens": max_tokens,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HARASSMENT",         "threshold": "BLOCK_NONE"},
            ],
        }
        # Vision is supported by gemini-1.5-pro and gemini-1.5-flash
        url = f"{_GEMINI_API_BASE}/{self._model}:generateContent?key={self._api_key}"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            logger.error("Gemini vision response parse error: %s", json.dumps(data)[:400])
            raise ValueError("Could not extract vision analysis from Gemini response.") from exc

    async def generate_narrative(
        self, request: NarrativeRequestPayload
    ) -> NarrativeResponsePayload:
        """
        Send the mechanical truth + player intent + story memory to Gemini.
        The system prompt enforces the anti-sycophancy, mechanical truth, and
        story continuity locks so Gemini cannot hallucinate contradictions.
        """
        mechanical_truth_json = request.mechanical_truth.model_dump_json(indent=2)

        # Format established world facts as bullet lines for the continuity lock
        story_lines = [
            f"[{f.entity_type.value.upper()}] {f.entity_name}: {f.summary}"
            for f in request.story_context
        ] if request.story_context else []

        system_prompt = build_narrative_system_prompt(
            system=request.campaign_system,
            mechanical_truth_json=mechanical_truth_json,
            story_context_lines=story_lines,
        )

        user_content = (
            f"The player stated: \"{request.player_intent}\"\n\n"
            f"Character: {request.character_context.name} "
            f"({request.character_context.system})\n"
            f"Current Status: {request.character_context.status.value}\n\n"
            "Narrate the outcome."
        )

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_content}]}],
            "generationConfig": {
                "temperature": 0.85,
                "maxOutputTokens": 800,
                "topP": 0.95,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HARASSMENT",         "threshold": "BLOCK_NONE"},
            ],
        }

        url = f"{_GEMINI_API_BASE}/{self._model}:generateContent?key={self._api_key}"

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

        data = response.json()

        try:
            narrative_text = (
                data["candidates"][0]["content"]["parts"][0]["text"]
            )
        except (KeyError, IndexError) as exc:
            logger.error("Unexpected Gemini response structure: %s", json.dumps(data)[:500])
            raise ValueError("Could not extract narrative from Gemini response.") from exc

        # Derive a short embed title from the outcome
        outcome = request.mechanical_truth.outcome.value.replace("_", " ").title()
        char_name = request.character_context.name
        embed_title = f"{char_name}: {outcome}"

        return NarrativeResponsePayload(
            prompt_id=request.prompt_id,
            intent_id=request.intent_id,
            narrative=narrative_text,
            embed_title=embed_title,
            multimedia=[],  # multimedia cue selection happens in the pipeline
        )

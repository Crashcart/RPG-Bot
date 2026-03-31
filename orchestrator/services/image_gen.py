"""
Ironclad GM – Image Generation Service
========================================
Pluggable image generation with three backends:

  disabled      — no-op (default; safe until GPU / API key configured)
  comfyui       — local ComfyUI Docker service (Intel GPU via /dev/dri)
  stability_ai  — Stability AI v2beta cloud API
  dalle3        — OpenAI DALL-E 3 API

Backend is selected at runtime from the 'image_gen_backend' system_setting.
All generated images are saved to /app/assets/gen/{uuid}.png and served via
the media-proxy so Discord can embed them directly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import httpx

from orchestrator.config import get_settings

logger  = logging.getLogger(__name__)
settings = get_settings()

_GEN_DIR  = Path("/app/assets/gen")
_PORTRAIT_DIR = Path("/app/assets/portraits")

# ── ComfyUI basic text2img workflow ──────────────────────────────────────────
# Minimal workflow: KSampler → VAEDecode → SaveImage
# Injected prompt replaces the CLIPTextEncode node's text.
_COMFYUI_WORKFLOW_TEMPLATE: dict = {
    "3": {"inputs": {"seed": 0, "steps": 20, "cfg": 7, "sampler_name": "euler",
                     "scheduler": "normal", "denoise": 1,
                     "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                     "latent_image": ["5", 0]},
          "class_type": "KSampler"},
    "4": {"inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"},
          "class_type": "CheckpointLoaderSimple"},
    "5": {"inputs": {"width": 512, "height": 512, "batch_size": 1},
          "class_type": "EmptyLatentImage"},
    "6": {"inputs": {"text": "PROMPT_PLACEHOLDER", "clip": ["4", 1]},
          "class_type": "CLIPTextEncode"},
    "7": {"inputs": {"text": "ugly, blurry, low quality, watermark, text", "clip": ["4", 1]},
          "class_type": "CLIPTextEncode"},
    "8": {"inputs": {"samples": ["3", 0], "vae": ["4", 2]},
          "class_type": "VAEDecode"},
    "9": {"inputs": {"filename_prefix": "ironclad", "images": ["8", 0]},
          "class_type": "SaveImage"},
}


class ImageGenService:
    """
    Generates images from text prompts using the configured backend.
    All methods return a media-proxy URL string or None if generation fails
    or the backend is disabled.
    """

    def __init__(self, db=None) -> None:
        """
        Args:
            db: DatabaseService instance for reading system_settings at runtime.
        """
        self._db          = db
        self._media_url   = settings.media_proxy_url
        self._comfyui_url = settings.comfyui_url
        self._client      = httpx.AsyncClient(timeout=120.0)
        _GEN_DIR.mkdir(parents=True, exist_ok=True)
        _PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt:    str,
        width:     int = 768,
        height:    int = 512,
        filename:  str | None = None,
    ) -> str | None:
        """
        Generate a scene image from a text prompt.

        Returns a media-proxy URL or None.
        """
        backend = await self._get_backend()
        if backend == "disabled":
            return None

        out_name = filename or f"{uuid.uuid4()}.png"
        out_path = _GEN_DIR / out_name

        if backend == "comfyui":
            return await self._generate_comfyui(prompt, width, height, out_path)
        elif backend == "stability_ai":
            return await self._generate_stability(prompt, width, height, out_path)
        elif backend == "dalle3":
            return await self._generate_dalle3(prompt, out_path)
        else:
            logger.warning("Unknown image_gen_backend '%s' — skipping", backend)
            return None

    async def generate_npc_portrait(
        self,
        npc_name:    str,
        description: str,
        campaign_id: str,
    ) -> str | None:
        """
        Generate a portrait for an NPC. Saves with a stable filename based on
        npc_name + campaign so the same portrait is reused across sessions.
        Returns media-proxy URL or None.
        """
        safe_name = "".join(c if c.isalnum() else "_" for c in npc_name.lower())
        filename  = f"portrait_{campaign_id[:8]}_{safe_name}.png"
        full_path = _PORTRAIT_DIR / filename

        if full_path.exists():
            return f"{self._media_url}/portraits/{filename}"

        portrait_prompt = (
            f"Portrait of {npc_name}: {description}. "
            "Head and shoulders, dramatic lighting, fantasy art style, "
            "detailed face, high quality."
        )
        result = await self.generate(portrait_prompt, width=512, height=512,
                                     filename=f"../portraits/{filename}")
        return result

    # ------------------------------------------------------------------
    # Backends
    # ------------------------------------------------------------------

    async def _generate_comfyui(
        self, prompt: str, width: int, height: int, out_path: Path
    ) -> str | None:
        """POST to ComfyUI's /prompt endpoint, poll /history until complete."""
        import copy
        import random

        workflow = copy.deepcopy(_COMFYUI_WORKFLOW_TEMPLATE)
        workflow["3"]["inputs"]["seed"]          = random.randint(0, 2**32)
        workflow["5"]["inputs"]["width"]         = width
        workflow["5"]["inputs"]["height"]        = height
        workflow["6"]["inputs"]["text"]          = prompt

        try:
            # Queue the prompt
            resp = await self._client.post(
                f"{self._comfyui_url}/prompt",
                json={"prompt": workflow},
            )
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]

            # Poll /history/{prompt_id} until outputs are ready (max 60 s)
            for _ in range(60):
                await asyncio.sleep(1)
                hist = await self._client.get(f"{self._comfyui_url}/history/{prompt_id}")
                if hist.status_code == 200:
                    data = hist.json()
                    if prompt_id in data and data[prompt_id].get("outputs"):
                        outputs = data[prompt_id]["outputs"]
                        # Find the SaveImage node output
                        for node_id, node_out in outputs.items():
                            if "images" in node_out:
                                img_info = node_out["images"][0]
                                # Download the image
                                img_resp = await self._client.get(
                                    f"{self._comfyui_url}/view",
                                    params={
                                        "filename": img_info["filename"],
                                        "subfolder": img_info.get("subfolder", ""),
                                        "type":     img_info.get("type", "output"),
                                    },
                                )
                                img_resp.raise_for_status()
                                out_path.write_bytes(img_resp.content)
                                rel = out_path.relative_to(Path("/app/assets"))
                                return f"{self._media_url}/{rel}"

            logger.warning("ComfyUI generation timed out for prompt: %s", prompt[:80])
            return None

        except Exception as exc:
            logger.error("ComfyUI generation error: %s", exc)
            return None

    async def _generate_stability(
        self, prompt: str, width: int, height: int, out_path: Path
    ) -> str | None:
        """POST to Stability AI v2beta stable-image generate endpoint."""
        if not settings.stability_ai_key:
            logger.warning("Stability AI backend selected but STABILITY_AI_KEY not set")
            return None
        try:
            resp = await self._client.post(
                "https://api.stability.ai/v2beta/stable-image/generate/sd3",
                headers={
                    "authorization": f"Bearer {settings.stability_ai_key}",
                    "accept":        "image/*",
                },
                data={
                    "prompt": prompt,
                    "output_format": "png",
                    "width": width,
                    "height": height,
                },
            )
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
            rel = out_path.relative_to(Path("/app/assets"))
            return f"{self._media_url}/{rel}"
        except Exception as exc:
            logger.error("Stability AI generation error: %s", exc)
            return None

    async def _generate_dalle3(self, prompt: str, out_path: Path) -> str | None:
        """Generate via OpenAI DALL-E 3 API."""
        if not settings.openai_api_key:
            logger.warning("DALL-E 3 backend selected but OPENAI_API_KEY not set")
            return None
        try:
            resp = await self._client.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":   "dall-e-3",
                    "prompt":  prompt,
                    "n":       1,
                    "size":    "1024x1024",
                    "quality": "standard",
                },
            )
            resp.raise_for_status()
            image_url = resp.json()["data"][0]["url"]
            # Download the image and cache locally
            img = await self._client.get(image_url)
            img.raise_for_status()
            out_path.write_bytes(img.content)
            rel = out_path.relative_to(Path("/app/assets"))
            return f"{self._media_url}/{rel}"
        except Exception as exc:
            logger.error("DALL-E 3 generation error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_backend(self) -> str:
        if self._db is not None:
            return await self._db.get_system_setting("image_gen_backend", "disabled")
        return "disabled"

    async def close(self) -> None:
        await self._client.aclose()

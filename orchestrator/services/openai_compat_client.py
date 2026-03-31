"""
Ironclad GM – OpenAI-Compatible Cloud Adjudication Client
==========================================================
Covers Groq, OpenRouter, Together AI, and SillyTavern — all expose the
OpenAI Chat Completions API format.  Used as a drop-in replacement for
OllamaClient when adjudication_provider != 'ollama'.

SillyTavern note
----------------
SillyTavern is NOT installed as part of this stack.  The 'sillytavern'
provider connects to an EXISTING external SillyTavern instance at a
user-configured URL.  SillyTavern itself proxies to whatever model the
user has selected in its UI (Ollama, KoboldAI, Claude, etc.), so no
model name or API key is required on our side unless the user has
enabled SillyTavern's API key protection.

Typical SillyTavern endpoint: http://<host>:8000/api/openai/v1
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

from orchestrator.config import get_settings

if TYPE_CHECKING:
    from orchestrator.schemas.payloads import (
        ContextAssemblyPayload,
        OllamaResolutionPayload,
    )

logger = logging.getLogger(__name__)

settings = get_settings()

# Fixed-URL cloud providers
_PROVIDER_BASE_URLS: dict[str, str] = {
    "groq":       "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together":   "https://api.together.xyz/v1",
}

_PROVIDER_API_KEYS: dict[str, str] = {
    "groq":       settings.groq_api_key,
    "openrouter": settings.openrouter_api_key,
    "together":   settings.together_api_key,
}

_PROVIDER_MODELS: dict[str, str] = {
    "groq":       settings.groq_model,
    "openrouter": settings.openrouter_model,
    "together":   settings.together_model,
}

# All known provider names (including the dynamic-URL sillytavern)
_ALL_PROVIDERS = set(_PROVIDER_BASE_URLS) | {"sillytavern"}


class OpenAICompatClient:
    """
    Thin async client for OpenAI-compatible inference providers.

    Implements the same generate() and resolve_action() interface as
    OllamaClient so NodeRouter can swap it in transparently.

    Supports:
      • groq       — Groq cloud (fixed URL, API key required)
      • openrouter — OpenRouter cloud (fixed URL, API key required)
      • together   — Together AI cloud (fixed URL, API key required)
      • sillytavern — External SillyTavern instance (dynamic URL, no key by default)
    """

    def __init__(self, provider: str, base_url: str | None = None) -> None:
        """
        Args:
            provider: One of 'groq', 'openrouter', 'together', 'sillytavern'.
            base_url: Override the base URL.  Required when provider='sillytavern'
                      and sillytavern_url is not set in config.
        """
        if provider not in _ALL_PROVIDERS:
            raise ValueError(
                f"Unknown provider '{provider}'. Valid: {sorted(_ALL_PROVIDERS)}"
            )
        self._provider = provider

        if provider == "sillytavern":
            url = base_url or settings.sillytavern_url
            if not url:
                raise ValueError(
                    "SillyTavern URL is not configured.  Set SILLYTAVERN_URL in .env "
                    "or configure it in White Portal → Settings → Inference Engine."
                )
            self._base_url = url.rstrip("/")
            # SillyTavern uses whatever model is selected in its own UI.
            # An optional hint can be passed; if blank, omit the field entirely.
            self._model    = settings.sillytavern_model or None
            headers: dict[str, str] = {"Content-Type": "application/json"}
            api_key = settings.sillytavern_api_key
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            self._base_url = _PROVIDER_BASE_URLS[provider]
            self._model    = _PROVIDER_MODELS[provider]
            headers = {
                "Authorization": f"Bearer {_PROVIDER_API_KEYS[provider]}",
                "Content-Type":  "application/json",
            }
            if provider == "openrouter":
                headers["HTTP-Referer"] = "https://ironclad-gm.local"
                headers["X-Title"]      = "Ironclad GM"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=60.0,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> dict:
        """Construct the /chat/completions request body."""
        payload: dict = {
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }
        # Include model only when we have one — SillyTavern uses its own active model
        if self._model:
            payload["model"] = self._model
        return payload

    async def generate(
        self,
        system: str,
        user: str,
        max_tokens: int = 800,
        temperature: float = 0.7,
    ) -> str:
        """Free-form generation — mirrors OllamaClient.generate()."""
        payload = self._build_payload(
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def resolve_action(
        self,
        ctx: "ContextAssemblyPayload",
        dice_result: int,
        system_prompt: str,
        user_prompt: str,
    ) -> "OllamaResolutionPayload":
        """
        Mechanical adjudication — mirrors OllamaClient.resolve_action().
        Requests JSON output via response_format when supported (Groq/OpenAI
        support it; Together/OpenRouter/SillyTavern receive the instruction
        in the prompt instead).
        """
        from orchestrator.schemas.payloads import OllamaResolutionPayload

        payload = self._build_payload(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=1024,
            temperature=0.1,
        )
        # Groq natively supports response_format JSON mode
        if self._provider in ("groq",):
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(2):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
                data = json.loads(raw)
                return OllamaResolutionPayload(**data)
            except (json.JSONDecodeError, Exception) as exc:
                if attempt == 1:
                    logger.error(
                        "[%s] resolve_action JSON parse failed after 2 attempts: %s",
                        self._provider, exc,
                    )
                    raise
                logger.warning(
                    "[%s] resolve_action attempt %d failed: %s — retrying",
                    self._provider, attempt + 1, exc,
                )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Compatibility shims (used by NodeRouter health checks)
    # ------------------------------------------------------------------

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model or ""

    def is_available(self) -> bool:
        """
        Returns True if the provider is ready to handle requests.

        For sillytavern: True when a URL is configured (no API key needed).
        For cloud providers: True when an API key is configured.
        """
        if self._provider == "sillytavern":
            return bool(self._base_url)
        return bool(_PROVIDER_API_KEYS.get(self._provider))

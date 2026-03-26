"""Client for the Anthropic Claude narrative generation API.

Drop-in alternative to GeminiClient — exposes the same generate() and
generate_narrative() interface so GMDirector can call either transparently.

Activate by setting:
    CLAUDE_API_KEY=sk-ant-...
    CLOUD_PROVIDER=claude
in your .env file.
"""

from __future__ import annotations

import json
import logging

import httpx

from orchestrator.config import Settings
from orchestrator.prompts.guardrails import build_narrative_system_prompt
from orchestrator.schemas.payloads import NarrativeRequestPayload, NarrativeResponsePayload

logger = logging.getLogger(__name__)

_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


class ClaudeClient:
    """
    Anthropic Claude client for Tier 1 storyteller narration.

    Mirrors GeminiClient.generate() and GeminiClient.generate_narrative()
    so it can be swapped into GMDirector._select_storyteller() without any
    other code changes.
    """

    def __init__(self, settings: Settings) -> None:
        self._api_key   = settings.claude_api_key
        self._model     = settings.claude_model
        self._node_name = "claude-cloud"

    # ── Generic text generation (used by GMDirector) ─────────────────────────

    async def generate(
        self,
        system_prompt: str,
        user_prompt:   str,
        max_tokens:    int = 800,
    ) -> str:
        """
        Low-level free-form text generation via Claude.

        Mirrors GeminiClient.generate() so the GMDirector can call either
        transparently for planning and synthesis passes.

        Args:
            system_prompt: System prompt text.
            user_prompt:   User-turn content.
            max_tokens:    Maximum tokens to generate.

        Returns:
            The generated text, stripped of leading/trailing whitespace.
        """
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(_CLAUDE_API_URL, json=payload, headers=headers)
            response.raise_for_status()

        data = response.json()
        try:
            return data["content"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            logger.error("Unexpected Claude response in generate(): %s", json.dumps(data)[:400])
            raise ValueError("Could not extract text from Claude response.") from exc

    # ── Narrative generation (Phase 4) ───────────────────────────────────────

    async def generate_narrative(
        self, request: NarrativeRequestPayload
    ) -> NarrativeResponsePayload:
        """
        Send the mechanical truth + player intent + story memory to Claude.
        The system prompt enforces the anti-sycophancy, mechanical truth, and
        story continuity locks so Claude cannot hallucinate contradictions.

        Mirrors GeminiClient.generate_narrative().
        """
        mechanical_truth_json = request.mechanical_truth.model_dump_json(indent=2)

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
            "model": self._model,
            "max_tokens": 800,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(_CLAUDE_API_URL, json=payload, headers=headers)
            response.raise_for_status()

        data = response.json()
        try:
            narrative_text = data["content"][0]["text"]
        except (KeyError, IndexError) as exc:
            logger.error("Unexpected Claude response structure: %s", json.dumps(data)[:500])
            raise ValueError("Could not extract narrative from Claude response.") from exc

        outcome = request.mechanical_truth.outcome.value.replace("_", " ").title()
        char_name = request.character_context.name
        embed_title = f"{char_name}: {outcome}"

        return NarrativeResponsePayload(
            prompt_id=request.prompt_id,
            intent_id=request.intent_id,
            narrative=narrative_text,
            embed_title=embed_title,
            multimedia=[],
        )

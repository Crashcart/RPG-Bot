"""Client for the Google Gemini narrative generation API."""

from __future__ import annotations

import json
import logging

import httpx

from orchestrator.config import Settings
from orchestrator.prompts.guardrails import build_narrative_system_prompt
from orchestrator.schemas.payloads import NarrativeRequestPayload, NarrativeResponsePayload

logger = logging.getLogger(__name__)

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.gemini_api_key
        self._model = settings.gemini_model

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

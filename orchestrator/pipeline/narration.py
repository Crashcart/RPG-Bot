"""
Phase 4 – Narrative Generation
================================
Constructs the Gemini prompt from the unalterable mechanical truth and
the player's original intent. Gemini is the storyteller; it must not
contradict a single mechanical fact produced in Phase 2.
"""

from __future__ import annotations

import logging

from orchestrator.schemas.payloads import (
    MechanicalTruth,
    NarrativeRequestPayload,
    NarrativeResponsePayload,
    OllamaResolutionPayload,
    StateCommitPayload,
    CharacterSnapshot,
)
from orchestrator.services.gemini_client import GeminiClient

logger = logging.getLogger(__name__)


class NarrationPhase:
    def __init__(self, gemini: GeminiClient) -> None:
        self._gemini = gemini

    async def narrate(
        self,
        resolution: OllamaResolutionPayload,
        commit: StateCommitPayload,
        character: CharacterSnapshot,
        player_intent: str,
        campaign_system: str,
    ) -> NarrativeResponsePayload:
        """
        Build the narrative request from mechanical facts and call Gemini.
        The mechanical truth is locked and must be honoured by the narrator.
        """
        truth = MechanicalTruth(
            action_type=resolution.action_type,
            difficulty=resolution.difficulty,
            dice_notation=resolution.dice_request.notation,
            roll_result=resolution.roll_result,
            outcome=resolution.outcome,
            stat_changes=resolution.state_delta.stat_deltas,
            status_change=commit.status_change,
            rulebook_citations=resolution.rulebook_citations,
        )

        request = NarrativeRequestPayload(
            intent_id=resolution.intent_id,
            player_intent=player_intent,
            mechanical_truth=truth,
            character_context=character,
            campaign_system=campaign_system,
        )

        narrative = await self._gemini.generate_narrative(request)

        logger.info(
            "Phase 4 complete: narrative=%d chars, lethal=%s",
            len(narrative.narrative),
            commit.lethal,
        )

        return narrative

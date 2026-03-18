"""
Phase 4 – Narrative Generation
================================
Constructs the Gemini prompt from:
  - the unalterable mechanical truth (Phase 2)
  - the player's original intent
  - story memory context retrieved from the campaign journal

After Gemini returns the narrative, the story memory service extracts
any newly established world facts and persists them so future calls
are grounded in continuity.
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
from orchestrator.services.story_memory import StoryMemoryService

logger = logging.getLogger(__name__)


class NarrationPhase:
    def __init__(self, gemini: GeminiClient, story_memory: StoryMemoryService) -> None:
        self._gemini       = gemini
        self._story_memory = story_memory

    async def narrate(
        self,
        resolution: OllamaResolutionPayload,
        commit: StateCommitPayload,
        character: CharacterSnapshot,
        player_intent: str,
        campaign_system: str,
        campaign_id: str,
    ) -> NarrativeResponsePayload:
        """
        1. Retrieve relevant story facts from the campaign journal.
        2. Build the narrative request (mechanical truth + story context).
        3. Call Gemini — it is bound by all three locks.
        4. Extract new facts from the generated narrative and persist them.
        """
        # ── Step 1: Retrieve story memory ─────────────────────────────────────
        story_context = await self._story_memory.retrieve_relevant_context(
            query=player_intent,
            campaign_id=campaign_id,
        )
        logger.debug(
            "Phase 4: %d story facts loaded for campaign %s",
            len(story_context), campaign_id,
        )

        # ── Step 2: Build mechanical truth block ──────────────────────────────
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
            story_context=story_context,
        )

        # ── Step 3: Generate narrative (all three locks enforced) ─────────────
        narrative = await self._gemini.generate_narrative(request)

        # ── Step 4: Extract new facts and persist to story memory ─────────────
        new_facts = await self._story_memory.extract_and_store(
            narrative=narrative.narrative,
            campaign_id=campaign_id,
            intent_id=resolution.intent_id,
        )
        logger.info(
            "Phase 4 complete: narrative=%d chars, lethal=%s, new_facts=%d",
            len(narrative.narrative),
            commit.lethal,
            len(new_facts),
        )

        return narrative

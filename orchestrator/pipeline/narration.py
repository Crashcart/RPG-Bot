"""
Phase 4 – Narrative Generation
================================
Constructs the narrative from the unalterable mechanical truth, the player's
original intent, and campaign story memory.

Storyteller Selection
---------------------
The "Enable Cloud Storyteller" system setting controls which engine runs
Phase 4:

  ON  (default) → Gemini API (IRONCLAD-NARRATOR with full cloud guardrails)
  OFF           → Highest-priority local Ollama node with role='narrative'
                  (IRONCLAD-NARRATOR LOCAL with uncensored mode granted)

Graceful degradation order when the toggle is OFF:
  1. Best available 'narrative'-tagged Ollama node
  2. If no narrative node: log a warning and fall back to Gemini anyway
     (better a result than a crash — operators should tag a node)

After narrative generation, new world facts are extracted and persisted to the
story_context table so future calls are grounded in continuity.
Fact extraction uses Gemini; if the Cloud Storyteller is OFF and extraction
fails, it is skipped silently (continuity is best-effort in local mode).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestrator.schemas.payloads import (
    CharacterSnapshot,
    MechanicalTruth,
    NarrativeRequestPayload,
    NarrativeResponsePayload,
    OllamaResolutionPayload,
    StateCommitPayload,
)
from orchestrator.services.gemini_client import GeminiClient
from orchestrator.services.story_memory import StoryMemoryService

if TYPE_CHECKING:
    from orchestrator.services.node_router import NodeRouter

logger = logging.getLogger(__name__)

_NARRATIVE_ROLE = "narrative"


class NarrationPhase:
    def __init__(
        self,
        gemini:       GeminiClient,
        story_memory: StoryMemoryService,
        node_router:  "NodeRouter | None" = None,
    ) -> None:
        self._gemini       = gemini
        self._story_memory = story_memory
        self._node_router  = node_router

    async def narrate(
        self,
        resolution:      OllamaResolutionPayload,
        commit:          StateCommitPayload,
        character:       CharacterSnapshot,
        player_intent:   str,
        campaign_system: str,
        campaign_id:     str,
    ) -> NarrativeResponsePayload:
        """
        1. Retrieve relevant story facts from the campaign journal.
        2. Build the narrative request (mechanical truth + story context).
        3. Route to Gemini (cloud) or local Ollama (local) per system setting.
        4. Extract new facts from the generated narrative and persist them.
        """
        # ── Step 1: Story memory retrieval ────────────────────────────────────
        story_context = await self._story_memory.retrieve_relevant_context(
            query=player_intent,
            campaign_id=campaign_id,
        )
        logger.debug(
            "Phase 4: %d story facts loaded for campaign %s",
            len(story_context), campaign_id,
        )

        # ── Step 2: Mechanical truth block ────────────────────────────────────
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

        # ── Step 3: Route to Storyteller ──────────────────────────────────────
        use_cloud = True
        if self._node_router:
            use_cloud = await self._node_router.is_storyteller_enabled()

        if use_cloud:
            narrative = await self._narrate_via_gemini(request)
        else:
            narrative = await self._narrate_via_local(request)

        # ── Step 4: Extract new world facts ───────────────────────────────────
        # Extraction always uses Gemini even in local mode (best-effort).
        # If Gemini is unavailable in local mode this is skipped silently.
        new_facts: list = []
        try:
            new_facts = await self._story_memory.extract_and_store(
                narrative=narrative.narrative,
                campaign_id=campaign_id,
                intent_id=resolution.intent_id,
            )
        except Exception as exc:
            logger.warning(
                "Phase 4: fact extraction failed (local mode? Gemini unavailable?): %s", exc
            )

        logger.info(
            "Phase 4 complete: engine=%s narrative=%d chars lethal=%s new_facts=%d",
            "gemini" if use_cloud else "local",
            len(narrative.narrative),
            commit.lethal,
            len(new_facts),
        )

        return narrative

    # ── Private routing methods ───────────────────────────────────────────────

    async def _narrate_via_gemini(
        self, request: NarrativeRequestPayload
    ) -> NarrativeResponsePayload:
        """Cloud path — Gemini with full guardrails."""
        logger.debug("Phase 4: Cloud Storyteller (Gemini) active.")
        return await self._gemini.generate_narrative(request)

    async def _narrate_via_local(
        self, request: NarrativeRequestPayload
    ) -> NarrativeResponsePayload:
        """
        Local path — Auto-Promotion Protocol.

        Calls NodeRouter.get_storyteller_client() which sorts available
        'narrative'-tagged nodes by their most recently measured TTFT
        (latency_ms ASC) rather than static priority.  Whichever node
        responded fastest in the last benchmark wins this turn, even if a
        higher-priority node exists that is currently under load.

        Fallback chain:
          1. Fastest narrative-tagged node (TTFT order)
          2. Gemini + operator warning (no narrative node available)
        """
        if self._node_router is None:
            logger.warning(
                "Phase 4: Cloud Storyteller is OFF but no NodeRouter available. "
                "Falling back to Gemini."
            )
            return await self._narrate_via_gemini(request)

        # Auto-Promotion: latency-sorted node selection
        local_client = await self._node_router.get_storyteller_client()

        if local_client is None:
            logger.warning(
                "Phase 4: Auto-Promotion found no available narrative node. "
                "Falling back to Gemini. Tag at least one node with role='narrative' "
                "in the White Portal to enable fully-local uncensored operation."
            )
            return await self._narrate_via_gemini(request)

        logger.info(
            "Phase 4: Local Storyteller active via Auto-Promotion — "
            "node=%s (uncensored mode).",
            local_client._base_url,
        )
        return await local_client.generate_narrative(request)

"""
Phase 4 – Narrative Generation (GM Director)
=============================================
Entry point for the two-tier storyteller architecture.

NarrationPhase is now a thin delegator: it receives the outputs of Phases 1–3
and hands control to the GMDirector, which runs the full planning → delegation
→ synthesis pipeline.

The GMDirector handles:
  • Cloud vs. local storyteller selection (Cloud Storyteller toggle)
  • Sub-agent dispatch to local Ollama nodes
  • Character sheet gate (only exposes stat changes when something changed)
  • Structural text filter (strips accidental headings/lists from synthesis)
  • Story fact extraction and persistence (post-synthesis)

See orchestrator/services/gm_director.py for the full architecture.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestrator.schemas.payloads import (
    CharacterSnapshot,
    NarrativeResponsePayload,
    OllamaResolutionPayload,
    StateCommitPayload,
)

if TYPE_CHECKING:
    from orchestrator.services.gm_director import GMDirector

logger = logging.getLogger(__name__)


class NarrationPhase:
    def __init__(self, gm_director: "GMDirector") -> None:
        self._gm = gm_director

    async def narrate(
        self,
        resolution:      OllamaResolutionPayload,
        commit:          StateCommitPayload,
        character:       CharacterSnapshot,
        player_intent:   str,
        campaign_system: str,
        campaign_id:     str,
    ) -> NarrativeResponsePayload:
        """Delegate Phase 4 entirely to the GM Director."""
        return await self._gm.narrate(
            resolution=resolution,
            commit=commit,
            character=character,
            player_intent=player_intent,
            campaign_system=campaign_system,
            campaign_id=campaign_id,
        )

"""
Phase 1 – Ingestion & Context Assembly
=======================================
Assembles the full context bundle (character state + inventory + vehicle/asset
state + rulebook chunks + Rolling Vault history) that will be sent to the
Ollama mechanical engine.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestrator.schemas.payloads import (
    CharacterSnapshot,
    ContextAssemblyPayload,
    IntentPayload,
)
from orchestrator.services.database import DatabaseService
from orchestrator.services.rag_service import RAGService

if TYPE_CHECKING:
    from orchestrator.services.rolling_vault import RollingVault

logger = logging.getLogger(__name__)

# Keywords that suggest a vehicle/asset is involved in the action.
# When ANY of these appear in the player's raw input, vehicle context is
# loaded from the DB and injected into the Ollama prompt.
_VEHICLE_KEYWORDS = frozenset({
    "ship", "vessel", "gunner", "pilot", "helm", "cockpit", "turret",
    "cannon", "torpedo", "missile", "hull", "engine", "shields", "mech",
    "vehicle", "tank", "station", "seat", "station", "bay", "hangar",
    "autopilot", "drive", "port", "starboard", "bow", "stern",
    "fire", "shoot", "target", "navigate",
})


def _action_involves_vehicle(raw_input: str) -> bool:
    lower = raw_input.lower()
    return any(kw in lower for kw in _VEHICLE_KEYWORDS)


class IngestionPhase:
    def __init__(
        self,
        db:            DatabaseService,
        rag:           RAGService,
        rolling_vault: "RollingVault | None" = None,
    ) -> None:
        self._db            = db
        self._rag           = rag
        self._rolling_vault = rolling_vault

    async def assemble(self, intent: IntentPayload, campaign_id: str) -> ContextAssemblyPayload:
        """
        1. Retrieve the player's active character from PostgreSQL.
        2. Retrieve relevant rulebook chunks from ChromaDB via RAG.
        3. If the action involves a vehicle, pull all vehicle/subsystem state
           for the campaign and include it in the context.
        4. Fetch the Rolling Vault history block to prevent context overflow.
        5. Return a ContextAssemblyPayload for the mechanical engine.
        """
        # ── 1. Character & Inventory State ────────────────────────────────────
        character = await self._db.get_character_by_player(intent.player_id, campaign_id)
        if not character:
            raise ValueError(
                f"No active character found for player {intent.player_id} "
                f"in campaign {campaign_id}."
            )

        inventory = await self._db.get_inventory(character.character_id)

        # ── 2. Vehicle / Asset Context ────────────────────────────────────────
        vehicle_context: list[dict] = []
        if _action_involves_vehicle(intent.raw_input):
            vehicle_context = await self._db.get_vehicles_for_campaign(campaign_id)
            if vehicle_context:
                logger.info(
                    "Phase 1: including %d vehicle(s) in context for action '%s…'",
                    len(vehicle_context),
                    intent.raw_input[:60],
                )

        # ── 3. Rule Module Discovery ──────────────────────────────────────────
        rule_modules = await self._db.get_active_rule_modules(campaign_id)
        vector_collections = [
            m["chroma_collection"]
            for m in rule_modules
            if m["module_type"] == "vector" and m.get("chroma_collection")
        ]

        # ── 4. RAG Retrieval ──────────────────────────────────────────────────
        rule_chunks = []
        if vector_collections:
            rule_chunks = await self._rag.retrieve_rule_chunks(
                query=intent.raw_input,
                collection_names=vector_collections,
                n_results=6,
            )
        else:
            logger.warning("No vector rule collections active for campaign %s.", campaign_id)

        # ── 5. Rolling Vault — bounded session history ────────────────────────
        rolling_context = ""
        if self._rolling_vault:
            rolling_context = await self._rolling_vault.get_context_block(campaign_id)

        logger.info(
            "Phase 1 complete: character=%s rule_chunks=%d vehicles=%d vault=%s",
            character.name,
            len(rule_chunks),
            len(vehicle_context),
            "yes" if rolling_context else "empty",
        )

        return ContextAssemblyPayload(
            intent_id=intent.intent_id,
            character=character,
            inventory_snapshot=inventory,
            vehicle_context=vehicle_context,
            rule_chunks=rule_chunks,
            raw_input=intent.raw_input,
            rolling_context=rolling_context,
        )

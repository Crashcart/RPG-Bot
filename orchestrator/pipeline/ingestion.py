"""
Phase 1 – Ingestion & Context Assembly
=======================================
Assembles the full context bundle (character state + rulebook chunks)
that will be sent to the Ollama mechanical engine.
"""

from __future__ import annotations

import logging

from orchestrator.schemas.payloads import (
    CharacterSnapshot,
    ContextAssemblyPayload,
    IntentPayload,
)
from orchestrator.services.database import DatabaseService
from orchestrator.services.rag_service import RAGService

logger = logging.getLogger(__name__)


class IngestionPhase:
    def __init__(self, db: DatabaseService, rag: RAGService) -> None:
        self._db  = db
        self._rag = rag

    async def assemble(self, intent: IntentPayload, campaign_id: str) -> ContextAssemblyPayload:
        """
        1. Retrieve the player's active character from PostgreSQL.
        2. Retrieve relevant rulebook chunks from ChromaDB via RAG.
        3. Return a ContextAssemblyPayload for the mechanical engine.
        """
        # ── 1. Character & Inventory State ────────────────────────────────────
        character = await self._db.get_character_by_player(intent.player_id, campaign_id)
        if not character:
            raise ValueError(
                f"No active character found for player {intent.player_id} "
                f"in campaign {campaign_id}."
            )

        inventory = await self._db.get_inventory(character.character_id)

        # ── 2. Rule Module Discovery ──────────────────────────────────────────
        rule_modules = await self._db.get_active_rule_modules(campaign_id)
        vector_collections = [
            m["chroma_collection"]
            for m in rule_modules
            if m["module_type"] == "vector" and m.get("chroma_collection")
        ]

        # ── 3. RAG Retrieval ──────────────────────────────────────────────────
        rule_chunks = []
        if vector_collections:
            rule_chunks = await self._rag.retrieve_rule_chunks(
                query=intent.raw_input,
                collection_names=vector_collections,
                n_results=6,
            )
        else:
            logger.warning("No vector rule collections active for campaign %s.", campaign_id)

        logger.info(
            "Phase 1 complete: character=%s, rule_chunks=%d",
            character.name,
            len(rule_chunks),
        )

        return ContextAssemblyPayload(
            intent_id=intent.intent_id,
            character=character,
            inventory_snapshot=inventory,
            rule_chunks=rule_chunks,
            raw_input=intent.raw_input,
        )

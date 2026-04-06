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
    ActionCategory,
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

# ── Intent Classification Keywords ────────────────────────────────────────────
# Ordered from most-specific to least-specific; the first matching category wins.

_STEALTH_KEYWORDS = frozenset({
    "sneak", "hide", "shadow", "stalk", "skulk", "creep", "slink",
    "stealth", "conceal", "disappear", "slip away", "prowl", "blend in",
    "move silently", "keep low", "stay hidden", "avoid detection",
    "unseen", "unnoticed", "disguise",
})

_COMBAT_KEYWORDS = frozenset({
    "attack", "strike", "swing", "slash", "stab", "shoot", "punch",
    "kick", "cast", "spell", "hit", "smash", "bash", "charge",
    "grapple", "parry", "dodge", "block", "assault", "ambush",
    "draw sword", "draw weapon", "fire arrow", "fire bolt",
    "throw", "lunge", "cleave", "backstab",
})

_SAVING_THROW_KEYWORDS = frozenset({
    "resist", "save against", "saving throw", "constitution save",
    "dexterity save", "wisdom save", "will save", "fortitude save",
    "reflex save", "avoid the effect", "shake off", "endure",
})

_SKILL_CHECK_KEYWORDS = frozenset({
    "climb", "swim", "jump", "run", "perception", "investigate",
    "search", "listen", "spot", "pick lock", "lockpick", "disarm",
    "pickpocket", "sleight of hand", "acrobatics", "athletics",
    "persuade", "intimidate", "deceive", "bluff", "negotiate",
    "first aid", "heal", "medicine", "craft", "brew", "forge",
    "track", "survival", "navigate",
})

_SOCIAL_KEYWORDS = frozenset({
    "talk", "speak", "say", "ask", "convince", "beg", "threaten",
    "charm", "flirt", "lie", "haggle", "barter", "greet", "introduce",
    "question", "interrogate", "seduce", "befriend",
})

_EXPLORATION_KEYWORDS = frozenset({
    "explore", "look around", "examine", "inspect", "open door",
    "enter", "leave", "travel", "walk", "move to", "go to",
    "read", "study", "identify", "appraise", "loot", "search room",
    "check for traps", "scout",
})


def _action_involves_vehicle(raw_input: str) -> bool:
    lower = raw_input.lower()
    return any(kw in lower for kw in _VEHICLE_KEYWORDS)


def _classify_action_category(raw_input: str) -> ActionCategory:
    """
    Lightweight keyword router: intercepts free-form player input and maps it
    to the most likely mechanical resolution path.

    Ordered priority: stealth → combat → saving_throw → skill_check →
    social → exploration → unknown.

    Keeping this in Python (not in the LLM) is the "deterministic intent
    parsing" step described in TDR §2-B-1.  The Ollama engine still receives
    the category as context so it can apply the correct rulebook section.
    """
    lower = raw_input.lower()
    if any(kw in lower for kw in _STEALTH_KEYWORDS):
        return ActionCategory.STEALTH
    if any(kw in lower for kw in _COMBAT_KEYWORDS):
        return ActionCategory.COMBAT
    if any(kw in lower for kw in _SAVING_THROW_KEYWORDS):
        return ActionCategory.SAVING_THROW
    if any(kw in lower for kw in _SKILL_CHECK_KEYWORDS):
        return ActionCategory.SKILL_CHECK
    if any(kw in lower for kw in _SOCIAL_KEYWORDS):
        return ActionCategory.SOCIAL
    if any(kw in lower for kw in _EXPLORATION_KEYWORDS):
        return ActionCategory.EXPLORATION
    return ActionCategory.UNKNOWN


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

        # ── 6. Intent Classification ──────────────────────────────────────────
        action_category = _classify_action_category(intent.raw_input)

        logger.info(
            "Phase 1 complete: character=%s rule_chunks=%d vehicles=%d vault=%s category=%s",
            character.name,
            len(rule_chunks),
            len(vehicle_context),
            "yes" if rolling_context else "empty",
            action_category.value,
        )

        return ContextAssemblyPayload(
            intent_id=intent.intent_id,
            character=character,
            inventory_snapshot=inventory,
            vehicle_context=vehicle_context,
            rule_chunks=rule_chunks,
            raw_input=intent.raw_input,
            rolling_context=rolling_context,
            action_category=action_category,
        )

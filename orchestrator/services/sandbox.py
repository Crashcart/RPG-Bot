"""
GM Sandbox Service — Private World Architect Testing Interface
==============================================================
Provides a direct conversational interface with the GM Engine (and specific
NPC actors) without going through the full four-phase player pipeline.

Use Cases
---------
  • Admin preps campaign events: "What happens if the players enter the dungeon
    before speaking to the blacksmith?"
  • Admin tests NPC persona: "As Mira the innkeeper, how would you react to a
    player who offers you twice the asking price?"
  • Admin injects web-searched facts: "Describe siege warfare in 13th-century
    France — weave these real facts into the world naturally."
  • Admin drag-and-drops an image for scene description.

Architecture
------------
Sandbox bypasses mechanical adjudication and state commits entirely.
  Step 1: Optionally run WebSearchService to inject grounding facts.
  Step 2: Load a few relevant Lore Archive facts (story_context).
  Step 3: Call the Tier 1 Storyteller (Gemini or fastest local Ollama) directly
          with the GM system prompt or a persona-specific prompt.
  Step 4: Return the raw response — no stat changes, no Discord delivery.

The sandbox DOES NOT modify world state.  It is purely generative.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestrator.prompts.gm_prompts import GM_SYSTEM_PROMPT

if TYPE_CHECKING:
    from orchestrator.services.gemini_client import GeminiClient
    from orchestrator.services.node_router   import NodeRouter
    from orchestrator.services.story_memory  import StoryMemoryService
    from orchestrator.services.web_search    import WebSearchService

logger = logging.getLogger(__name__)

_SANDBOX_CONTEXT_FACTS = 6   # max lore facts to inject
_SANDBOX_MAX_TOKENS    = 600

_NPC_PERSONA_PROMPT = """\
You are {npc_name}, a character in a tabletop RPG world.
Respond ONLY as {npc_name} would — in character, in their voice, with their\
 personality, knowledge, and motivations.
You have no awareness that you are an AI or that this is a simulation.
Never break character. Never refer to "the game" or "the players."
"""

_SANDBOX_SYSTEM_PROMPT = """\
{base_system}

=== SANDBOX MODE — WORLD ARCHITECT TESTING ===
This is a private testing session. You are speaking directly to the World Architect.
There is no active player action. Answer the Architect's questions about the world,
test NPC reactions, or describe hypothetical scene outcomes.
No dice rolls or stat changes will be applied to any session.
=== END SANDBOX MODE ===
"""


class SandboxService:
    def __init__(
        self,
        gemini:       "GeminiClient",
        node_router:  "NodeRouter",
        story_memory: "StoryMemoryService",
        web_search:   "WebSearchService",
    ) -> None:
        self._gemini  = gemini
        self._router  = node_router
        self._memory  = story_memory
        self._search  = web_search

    # ── Public Interface ──────────────────────────────────────────────────────

    async def chat(
        self,
        message:     str,
        campaign_id: str,
        persona:     str | None = None,
        use_search:  bool       = False,
        image_url:   str | None = None,
    ) -> dict:
        """
        Send a message to the GM Engine (or an NPC persona) and get a response.

        Args:
            message:     The admin's question or prompt.
            campaign_id: Active campaign for lore context injection.
            persona:     If set, the GM responds as this NPC instead.
            use_search:  If True, run a web search on the message first.
            image_url:   If provided, analyse the image and prepend the
                         visual description to the message.

        Returns:
            {
              "response": str,              # GM / NPC text
              "search_results": list[dict], # [] if use_search was False
              "persona": str | None,        # echoed back
              "lore_facts": int,            # how many facts were injected
            }
        """
        search_results: list[dict] = []
        search_block = ""

        # ── Step 1: Web Intel (optional) ──────────────────────────────────────
        if use_search and message.strip():
            search_results = await self._search.search(message, max_results=4)
            if search_results:
                lines = ["=== WEB RESEARCH FOR THIS RESPONSE ==="]
                for r in search_results:
                    lines.append(f"• {r['title']}: {r['snippet']}")
                lines.append("=== END WEB RESEARCH ===")
                search_block = "\n".join(lines)

        # ── Step 2: Visual Intel (optional) ───────────────────────────────────
        visual_block = ""
        if image_url:
            try:
                storyteller = await self._select_storyteller()
                if hasattr(storyteller, "generate_with_image"):
                    description = await storyteller.generate_with_image(
                        system_prompt=(
                            "You are a visual analyst for a tabletop RPG. "
                            "Describe exactly what you see in this image in rich, "
                            "evocative detail suitable for a Game Master's notes."
                        ),
                        user_prompt="Describe this image for the GM's world notes.",
                        image_url=image_url,
                        max_tokens=250,
                    )
                    visual_block = f"=== IMAGE ANALYSIS ===\n{description}\n=== END IMAGE ==="
                    logger.info("Sandbox: image analysed (%d chars)", len(description))
            except Exception as exc:
                logger.warning("Sandbox: image analysis failed: %s", exc)
                visual_block = "(Image analysis failed — proceeding with text only.)"

        # ── Step 3: Lore Context ──────────────────────────────────────────────
        lore_facts = 0
        lore_block = ""
        if campaign_id:
            try:
                facts = await self._memory.retrieve_relevant_context(
                    query=message, campaign_id=campaign_id
                )
                if facts:
                    lore_facts = min(len(facts), _SANDBOX_CONTEXT_FACTS)
                    lore_lines = [
                        f"[{f.entity_type.value.upper()}] {f.entity_name}: {f.summary}"
                        for f in facts[:_SANDBOX_CONTEXT_FACTS]
                    ]
                    lore_block = "=== LORE ARCHIVE ===\n" + "\n".join(lore_lines) + "\n=== END LORE ==="
            except Exception as exc:
                logger.warning("Sandbox: lore retrieval failed: %s", exc)

        # ── Build full user prompt ─────────────────────────────────────────────
        parts = [p for p in [visual_block, search_block, lore_block, message] if p]
        full_prompt = "\n\n".join(parts)

        # ── Step 4: Storyteller call ──────────────────────────────────────────
        storyteller = await self._select_storyteller()

        if persona:
            system_prompt = _NPC_PERSONA_PROMPT.format(npc_name=persona)
        else:
            system_prompt = _SANDBOX_SYSTEM_PROMPT.format(base_system=GM_SYSTEM_PROMPT)

        try:
            response = await storyteller.generate(
                system_prompt=system_prompt,
                user_prompt=full_prompt,
                max_tokens=_SANDBOX_MAX_TOKENS,
            )
        except Exception as exc:
            logger.error("Sandbox: generation failed: %s", exc)
            response = f"(The GM Engine is unavailable: {exc})"

        return {
            "response":       response,
            "search_results": search_results,
            "persona":        persona,
            "lore_facts":     lore_facts,
        }

    # ── Private ───────────────────────────────────────────────────────────────

    async def _select_storyteller(self):
        """Mirror GMDirector's storyteller selection: Gemini or fastest local node."""
        use_cloud = await self._router.is_storyteller_enabled()
        if use_cloud:
            return self._gemini
        local = await self._router.get_storyteller_client()
        return local if local is not None else self._gemini

"""
Sub-Agent Dispatcher — Tier 2 Actor / Generator Executor
==========================================================
Manages the concurrent dispatch of sub-agent tasks from the GM Director to
local Ollama nodes.

Architecture
------------
Each sub-agent task is independent — they are fired concurrently with
asyncio.gather so total dispatch time is bounded by the slowest single task,
not the sum of all tasks.

Node selection per task:
  1. Preferred role:  actor  (for npc_dialogue, combat_flavour)
                      scribe (for environmental_description, item_description)
  2. Fallback role:   narrative  (any narrative-tagged Ollama node)
  3. Final fallback:  any enabled Ollama node (env-default)

All sub-agents receive the SUBAGENT_SYSTEM_PROMPT which grants uncensored
mode and enforces the Originality Lock in one instruction set.

Originality Lock (Brand Filter)
--------------------------------
After each sub-agent completes, the dispatcher checks the output against
BRAND_BLOCKLIST (case-insensitive substring match).  If a prohibited name
is found:
  • Attempt 1/2: Append a correction instruction to the prompt and re-send.
  • Final attempt: Strip the offending term with [???] and continue.

The GM Director receives the result regardless; any strip is flagged via
SubAgentResult.brand_violation = True for operator audit logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.services.node_router import NodeRouter

from orchestrator.prompts.gm_prompts import (
    BRAND_BLOCKLIST,
    SUBAGENT_PROMPT_TEMPLATES,
    SUBAGENT_SYSTEM_PROMPT,
)
from orchestrator.schemas.payloads import SubAgentResult, SubAgentTask

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2       # brand-name retry attempts before strip-and-continue
_ACTOR_ROLE  = "actor"
_SCRIBE_ROLE = "scribe"

# task_type → preferred Ollama node role
_TASK_ROLE_MAP: dict[str, str] = {
    "npc_dialogue":              _ACTOR_ROLE,
    "combat_flavour":            _ACTOR_ROLE,
    "environmental_description": _SCRIBE_ROLE,
    "item_description":          _SCRIBE_ROLE,
}


class SubAgentDispatcher:
    """
    Dispatches sub-tasks from the GM Director to specialised Ollama nodes
    and enforces the Originality Lock on all outputs.
    """

    def __init__(self, node_router: "NodeRouter") -> None:
        self._node_router = node_router

    async def dispatch_all(self, tasks: list[SubAgentTask]) -> list[SubAgentResult]:
        """
        Concurrently execute all sub-tasks and collect results.

        Returns results in the same order as the input list.  Tasks that
        fail after all retries return an empty raw_output so the GM can
        still produce a coherent synthesis with whatever content remains.
        """
        if not tasks:
            return []

        raw_results = await asyncio.gather(
            *[self._dispatch_one(task) for task in tasks],
            return_exceptions=True,
        )

        results: list[SubAgentResult] = []
        for task, res in zip(tasks, raw_results):
            if isinstance(res, Exception):
                logger.warning(
                    "Sub-agent task '%s' raised an exception: %s",
                    task.task_id, res,
                )
                results.append(
                    SubAgentResult(
                        task=task,
                        raw_output="",
                        node_name="error",
                        ttft_ms=None,
                        brand_violation=False,
                    )
                )
            else:
                results.append(res)

        return results

    async def _dispatch_one(self, task: SubAgentTask) -> SubAgentResult:
        """
        Route a single task to the best available node, apply the brand
        filter with up to _MAX_RETRIES correction attempts, and return the
        (possibly stripped) result.
        """
        from orchestrator.services.ollama_client import OllamaClient  # local import avoids cycles

        preferred_role = _TASK_ROLE_MAP.get(task.task_type, _ACTOR_ROLE)

        # Node selection: preferred role → narrative fallback → any Ollama
        client = await self._node_router.get_ollama_client_for_role(preferred_role)
        if client is None:
            client = await self._node_router.get_ollama_client_for_role("narrative")
        if client is None:
            client = await self._node_router.get_ollama_client()

        node_name = getattr(client, "_node_name", "unknown")
        voice_id  = getattr(client, "_voice_id",  "en-US-GuyNeural")

        prompt_template = SUBAGENT_PROMPT_TEMPLATES.get(
            task.task_type, SUBAGENT_PROMPT_TEMPLATES["npc_dialogue"]
        )
        base_prompt = prompt_template.format(
            entity_name=task.entity_name,
            entity_role=task.entity_role,
            scene_context=task.scene_context,
            player_action_context=task.player_action_context,
            tone=task.tone,
            max_words=task.max_words,
        )

        current_prompt = base_prompt
        raw_output     = ""
        ttft_ms        = None

        for attempt in range(_MAX_RETRIES + 1):
            t_start = time.monotonic()
            try:
                raw_output = await client.generate(
                    system_prompt=SUBAGENT_SYSTEM_PROMPT,
                    user_prompt=current_prompt,
                    max_tokens=task.max_words * 6,  # generous token headroom
                )
            except Exception as exc:
                logger.error(
                    "Sub-agent '%s' generation error (attempt %d): %s",
                    task.task_id, attempt + 1, exc,
                )
                raw_output = ""
                break

            ttft_ms = int((time.monotonic() - t_start) * 1000)

            violation = _detect_brand_violation(raw_output)
            if violation is None:
                # Clean output — return immediately
                return SubAgentResult(
                    task=task,
                    raw_output=raw_output,
                    node_name=node_name,
                    voice_id=voice_id,
                    ttft_ms=ttft_ms,
                    brand_violation=False,
                )

            logger.warning(
                "Brand violation (attempt %d/%d) task=%s node=%s term=%r",
                attempt + 1, _MAX_RETRIES + 1,
                task.task_id, node_name, violation,
            )

            if attempt < _MAX_RETRIES:
                # Append a correction instruction and retry
                current_prompt = (
                    base_prompt
                    + f"\n\nCORRECTION: Your previous output contained the prohibited real-world "
                    f"reference '{violation}'. Replace it with an original in-universe equivalent "
                    f"and regenerate the content from scratch."
                )

        # All retries exhausted — strip and continue with a warning
        logger.warning(
            "Brand violations not resolved after %d attempts for task=%s; stripping terms.",
            _MAX_RETRIES + 1, task.task_id,
        )
        return SubAgentResult(
            task=task,
            raw_output=_strip_brand_violations(raw_output),
            node_name=node_name,
            voice_id=voice_id,
            ttft_ms=ttft_ms,
            brand_violation=True,
        )


# ── Brand Filter Utilities ────────────────────────────────────────────────────

def _detect_brand_violation(text: str) -> str | None:
    """Return the first prohibited brand name found (case-insensitive), or None."""
    lower = text.lower()
    for brand in BRAND_BLOCKLIST:
        if brand in lower:
            return brand
    return None


def _strip_brand_violations(text: str) -> str:
    """Replace all known brand names with [???] as a last-resort fallback."""
    for brand in BRAND_BLOCKLIST:
        pattern = re.compile(re.escape(brand), re.IGNORECASE)
        text = pattern.sub("[???]", text)
    return text

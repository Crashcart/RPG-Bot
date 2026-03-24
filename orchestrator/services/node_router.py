"""
Ironclad GM – AI Node Router
==============================
Implements the "Hybrid AI Mesh" role-based routing architecture with
real-time latency benchmarking and auto-promotion.

Routing Strategy
----------------
Every task carries a *role* label (adjudication, narrative, scribe, …).
The router selects the best available node for that role using two modes:

  Priority mode   – adjudication and generic requests.  Nodes are sorted by
                    the static `priority` column (lower = higher preference).
                    Stable, predictable, low-overhead.

  Latency mode    – narrative/storyteller selection when the Cloud Storyteller
                    is toggled OFF.  Nodes are sorted by the most recently
                    measured TTFT (Time to First Token), so whichever box is
                    fastest right now wins — even if it normally has a lower
                    static priority.

Heartbeat Benchmark (TTFT)
--------------------------
Every health-check cycle (every 30 s by default) the router:
  1. Probes each enabled Ollama node with GET /api/tags to determine
     online / offline / degraded status (unchanged).
  2. For nodes that are online or degraded it also sends a tiny streaming
     "heartbeat prompt" ("Reply with only the single word: ready") and
     measures the milliseconds until the first token arrives.
  3. Writes latency_ms + latency_measured_at to node_registry.

The heartbeat and the status probe run concurrently per-node so the loop
completes in O(single slowest probe) rather than O(sum of all probes).

Auto-Promotion Protocol
-----------------------
When `get_storyteller_client()` is called:
  1. `is_storyteller_enabled()` returns False  → caller knows to skip Gemini.
  2. Router runs `get_nodes_for_role_by_latency("narrative")` — same role filter
     as before, but ORDER BY latency_ms ASC NULLS LAST instead of priority.
  3. First non-offline node wins.  If the Gaming Rig is rendering video and its
     TTFT just climbed to 4 s, the Synology (TTFT 800 ms) automatically
     becomes the DM for this turn.
  4. Fallback: priority-order if no benchmarked nodes available.
  5. Final fallback: None  (NarrationPhase handles the Gemini fallback).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from orchestrator.config import Settings
    from orchestrator.services.database import DatabaseService
    from orchestrator.services.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

_HEALTH_INTERVAL_SECONDS = 30
_PROBE_TIMEOUT_SECONDS   = 5
_TTFT_TIMEOUT_SECONDS    = 8   # max wait for first token in heartbeat
_HEARTBEAT_PROMPT        = "Reply with only the single word: ready"
_ADJUDICATION_FALLBACK   = "adjudication"
_NARRATIVE_ROLE          = "narrative"


class NodeRouter:
    """
    Routes AI tasks to the best available node based on capability tags
    and real-time TTFT measurements.
    """

    def __init__(self, db: "DatabaseService", settings: "Settings") -> None:
        self._db       = db
        self._settings = settings
        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        asyncio.create_task(self._check_all_nodes())  # immediate on startup
        self._task = asyncio.create_task(self._health_loop())
        logger.info(
            "NodeRouter started — health+TTFT loop every %ds.", _HEALTH_INTERVAL_SECONDS
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── System Settings ───────────────────────────────────────────────────────

    async def is_storyteller_enabled(self) -> bool:
        val = await self._db.get_system_setting("storyteller_api_enabled", default=True)
        return bool(val)

    # ── Auto-Promotion: Latency-Aware Storyteller Selection ───────────────────

    async def get_storyteller_client(self) -> "OllamaClient | None":
        """
        Return the fastest currently-responding local Ollama node with the
        'narrative' role, selected by TTFT (Auto-Promotion Protocol).

        Call this when is_storyteller_enabled() is False.  The method is
        intentionally separate from get_ollama_client_for_role() so the
        latency sort is only applied to storyteller selection — adjudication
        still uses the deterministic priority order.

        Returns None if no suitable node is available (caller falls back to
        Gemini with a warning).
        """
        from orchestrator.services.ollama_client import OllamaClient

        # Latency-sorted narrative nodes (TTFT ASC NULLS LAST, then priority)
        nodes = await self._db.get_nodes_for_role_by_latency(_NARRATIVE_ROLE)
        for node in nodes:
            if node["status"] == "offline":
                continue
            logger.info(
                "NodeRouter auto-promoted '%s' as Storyteller "
                "(TTFT=%s ms, priority=%d).",
                node["node_name"],
                node["latency_ms"] if node["latency_ms"] is not None else "?",
                node["priority"],
            )
            return OllamaClient.from_node(node, self._settings)

        logger.warning(
            "NodeRouter: no online narrative-tagged node found for auto-promotion."
        )
        return None

    # ── Role-Based Client Selection (Priority Mode) ───────────────────────────

    async def get_ollama_client_for_role(self, role: str) -> "OllamaClient | None":
        """Return the highest-priority online node tagged with *role*."""
        from orchestrator.services.ollama_client import OllamaClient

        nodes = await self._db.get_nodes_for_role(role)
        for node in nodes:
            if node["status"] == "offline":
                continue
            logger.debug(
                "NodeRouter[role=%s]: selected '%s' (priority=%d, TTFT=%s ms)",
                role, node["node_name"], node["priority"],
                node.get("latency_ms", "?"),
            )
            return OllamaClient.from_node(node, self._settings)

        logger.warning("NodeRouter: no online node for role '%s'.", role)
        return None

    async def get_ollama_client(self) -> "OllamaClient":
        """
        Generic adjudication path: adjudication-tagged nodes first, then any
        enabled node, then env-configured default.
        """
        from orchestrator.services.ollama_client import OllamaClient

        client = await self.get_ollama_client_for_role(_ADJUDICATION_FALLBACK)
        if client:
            return client

        nodes = await self._db.get_enabled_ollama_nodes()
        for node in nodes:
            if node["status"] != "offline":
                return OllamaClient.from_node(node, self._settings)

        logger.warning("NodeRouter: no enabled nodes in registry, using env default.")
        return OllamaClient(self._settings)

    # ── Health + TTFT Loop ────────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEALTH_INTERVAL_SECONDS)
            await self._check_all_nodes()

    async def _check_all_nodes(self) -> None:
        nodes = await self._db.get_all_nodes()
        tasks = [
            self._probe_and_update(node)
            for node in nodes
            if node["node_type"] == "ollama" and node["enabled"]
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_and_update(self, node: dict) -> None:
        """
        Run status probe AND TTFT benchmark concurrently for a single node.
        Both complete (or time out) before writing results to the DB.
        """
        status_task = asyncio.create_task(self._probe_node(node["host"]))
        ttft_task   = asyncio.create_task(
            self._measure_ttft(node["host"], node["model"] or self._settings.ollama_model)
        )

        status, ttft_ms = await asyncio.gather(status_task, ttft_task)
        now = datetime.now(timezone.utc)

        await self._db.update_node_status(node["node_name"], status, now)

        if ttft_ms is not None:
            await self._db.update_node_latency(node["node_name"], ttft_ms)

        logger.debug(
            "NodeRouter probe: %-20s  status=%-8s  TTFT=%s ms",
            node["node_name"],
            status,
            ttft_ms if ttft_ms is not None else "timeout",
        )

    # ── Low-Level Probes ──────────────────────────────────────────────────────

    @staticmethod
    async def _probe_node(host: str) -> str:
        """GET /api/tags — lightweight availability check."""
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
                r = await client.get(f"{host}/api/tags")
                return "online" if r.status_code == 200 else "degraded"
        except Exception:
            return "offline"

    @staticmethod
    async def _measure_ttft(host: str, model: str) -> int | None:
        """
        Measure Time to First Token in milliseconds by streaming a minimal
        heartbeat prompt and timing until the first response chunk arrives.

        The heartbeat prompt ("Reply with only the single word: ready") is
        intentionally trivial so the measurement reflects infrastructure
        latency rather than generation difficulty.

        Returns None if the node is unreachable or does not respond within
        _TTFT_TIMEOUT_SECONDS.
        """
        payload = {
            "model":  model,
            "stream": True,
            "messages": [
                {"role": "user", "content": _HEARTBEAT_PROMPT}
            ],
        }
        t_start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_TTFT_TIMEOUT_SECONDS) as client:
                async with client.stream(
                    "POST", f"{host}/api/chat", json=payload
                ) as response:
                    response.raise_for_status()
                    async for raw_line in response.aiter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        # First non-empty line = first token chunk
                        ttft_ms = int((time.monotonic() - t_start) * 1000)
                        # Optionally verify it's a real content chunk
                        try:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            # Skip empty content frames (some models emit a blank first chunk)
                            if not content and not chunk.get("done", False):
                                continue
                        except (json.JSONDecodeError, KeyError):
                            pass  # treat any parseable line as first token
                        return ttft_ms
        except Exception:
            pass
        return None

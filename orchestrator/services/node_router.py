"""
Ironclad GM – AI Node Router
==============================
Implements the "Hybrid AI Mesh" described in the Ollama Intel spec.

The NodeRouter maintains a live registry of all available AI backends:
  • Multiple local Ollama instances (Mini PC, Synology, etc.)
  • The Gemini cloud API (always available as the "Storyteller" fallback)

On every adjudication request it selects the best available Ollama node by:
  1. Sorting enabled Ollama nodes by ascending priority
  2. Skipping nodes whose last known status is 'offline'
  3. Falling back to the next candidate on connection failure
  4. Falling back to the env-configured default if the DB registry is empty

A background health-check task probes each registered Ollama node every
30 seconds via GET /api/tags and writes the result back to node_registry.
This keeps the Live Node Status dashboard current without blocking requests.
"""

from __future__ import annotations

import asyncio
import logging
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


class NodeRouter:
    """
    Selects the best available Ollama node for a mechanical adjudication call.

    Usage:
        router = NodeRouter(db, settings)
        await router.start()                         # begin background health loop
        client = await router.get_ollama_client()    # use in adjudication phase
        await router.stop()                          # on shutdown
    """

    def __init__(self, db: "DatabaseService", settings: "Settings") -> None:
        self._db       = db
        self._settings = settings
        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background health-check loop."""
        # Run an immediate probe so the UI shows live status on first page load
        asyncio.create_task(self._check_all_nodes())
        self._task = asyncio.create_task(self._health_loop())
        logger.info("NodeRouter started — health loop every %ds.", _HEALTH_INTERVAL_SECONDS)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Node Selection ────────────────────────────────────────────────────────

    async def get_ollama_client(self) -> "OllamaClient":
        """
        Return an OllamaClient pointed at the highest-priority online node.
        Falls back gracefully to the next candidate, then to the env default.
        """
        # Lazy import avoids circular dependency
        from orchestrator.services.ollama_client import OllamaClient

        nodes = await self._db.get_enabled_ollama_nodes()
        for node in nodes:  # already sorted by priority ASC in the DB query
            if node["status"] == "offline":
                continue
            logger.debug(
                "NodeRouter: selecting node '%s' (%s) priority=%d",
                node["node_name"], node["host"], node["priority"],
            )
            return OllamaClient.from_node(node, self._settings)

        # Nothing in DB — fall back to env-configured default
        logger.warning(
            "NodeRouter: no enabled Ollama nodes in registry, using env default."
        )
        return OllamaClient(self._settings)

    # ── Health Loop ───────────────────────────────────────────────────────────

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
        status = await self._probe_node(node["host"])
        now    = datetime.now(timezone.utc)
        await self._db.update_node_status(node["node_name"], status, now)
        logger.debug("NodeRouter probe: %s → %s", node["node_name"], status)

    @staticmethod
    async def _probe_node(host: str) -> str:
        """GET /api/tags — Ollama's health / model list endpoint."""
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
                r = await client.get(f"{host}/api/tags")
                return "online" if r.status_code == 200 else "degraded"
        except Exception:
            return "offline"

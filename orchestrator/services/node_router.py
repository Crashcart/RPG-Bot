"""
Ironclad GM – AI Node Router
==============================
Implements the "Hybrid AI Mesh" role-based routing architecture.

Routing Strategy
----------------
Every incoming task carries a *role* label (e.g. "adjudication", "narrative",
"scribe", "vision").  The router finds the highest-priority enabled Ollama node
that has been tagged with that role.  If no role-tagged node is available, it
falls back to the standard priority-ordered list, then finally to the
env-configured default node.

Role → Task Mapping
-------------------
  adjudication  Phase 2  – mechanical resolution (default for all nodes)
  narrative     Phase 4  – local storyteller (active when Cloud Storyteller OFF)
  scribe        Phase 4b – fact extraction / lore DB writing
  vision        future   – OCR / image analysis
  code_gen      future   – code generation tasks

Storyteller Toggle
------------------
The operator can flip "storyteller_api_enabled" in system_settings at runtime
via the White Portal.  The router exposes `is_storyteller_enabled()` which
reads the DB so the NarrationPhase can decide whether to call Gemini or promote
a local narrative node.

Health Loop
-----------
A background task probes every registered Ollama node via GET /api/tags every
30 seconds and writes status + last_seen to the node_registry table, keeping
the Live Node Status dashboard current.
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

# Roles considered acceptable as generic adjudication nodes when no specific
# role match exists (any node without roles still fills this).
_ADJUDICATION_FALLBACK = "adjudication"


class NodeRouter:
    """
    Routes AI tasks to the best available node based on capability tags.

    Usage:
        router = NodeRouter(db, settings)
        await router.start()

        # Role-based (specific capability required)
        client = await router.get_ollama_client_for_role("narrative")

        # Generic (any available node)
        client = await router.get_ollama_client()

        # Storyteller toggle
        if await router.is_storyteller_enabled():
            ...use Gemini...

        await router.stop()
    """

    def __init__(self, db: "DatabaseService", settings: "Settings") -> None:
        self._db       = db
        self._settings = settings
        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        asyncio.create_task(self._check_all_nodes())   # immediate probe on startup
        self._task = asyncio.create_task(self._health_loop())
        logger.info("NodeRouter started — health loop every %ds.", _HEALTH_INTERVAL_SECONDS)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── System Settings ───────────────────────────────────────────────────────

    async def is_storyteller_enabled(self) -> bool:
        """Return True if the Cloud Storyteller (Gemini) is enabled."""
        val = await self._db.get_system_setting("storyteller_api_enabled", default=True)
        return bool(val)

    # ── Role-Based Client Selection ───────────────────────────────────────────

    async def get_ollama_client_for_role(self, role: str) -> "OllamaClient | None":
        """
        Return the highest-priority online Ollama node tagged with *role*.
        Returns None if no suitable node is found (caller should fall back or
        raise an appropriate error).
        """
        from orchestrator.services.ollama_client import OllamaClient

        nodes = await self._db.get_nodes_for_role(role)
        for node in nodes:
            if node["status"] == "offline":
                continue
            logger.debug(
                "NodeRouter[role=%s]: selected node '%s' (priority=%d)",
                role, node["node_name"], node["priority"],
            )
            return OllamaClient.from_node(node, self._settings)

        logger.warning("NodeRouter: no online node found for role '%s'.", role)
        return None

    async def get_ollama_client(self) -> "OllamaClient":
        """
        Return the highest-priority online node (regardless of role tags).
        Nodes explicitly tagged 'adjudication' are preferred first; then any
        node sorted by priority; then the env-configured default.
        """
        from orchestrator.services.ollama_client import OllamaClient

        # 1. Try adjudication-tagged nodes first
        client = await self.get_ollama_client_for_role(_ADJUDICATION_FALLBACK)
        if client:
            return client

        # 2. Fall back to any enabled online node
        nodes = await self._db.get_enabled_ollama_nodes()
        for node in nodes:
            if node["status"] != "offline":
                logger.debug(
                    "NodeRouter[generic]: selected node '%s' (priority=%d)",
                    node["node_name"], node["priority"],
                )
                return OllamaClient.from_node(node, self._settings)

        # 3. Last resort: env-configured default
        logger.warning("NodeRouter: no enabled nodes in registry, using env default.")
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
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
                r = await client.get(f"{host}/api/tags")
                return "online" if r.status_code == 200 else "degraded"
        except Exception:
            return "offline"

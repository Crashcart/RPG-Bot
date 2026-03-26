"""
Telemetry Service — Live WebSocket Terminal
============================================
Broadcasts real-time pipeline events to all connected White Portal
admin clients via WebSocket.

Event Types Emitted
--------------------
  storyteller_selected  Tier 1 storyteller chosen for this turn
  planning_done         GM planning pass complete; N sub-tasks identified
  sub_agent_dispatch    Sub-agents dispatched to Ollama nodes
  synthesis_start       Tier 1 synthesis pass beginning
  synthesis_done        Final narrative ready; length, stripped-patterns count
  directive_fired       World Architect directive consumed into narrative
  pipeline_complete     Full turn complete; duration_ms and outcome
  campfire_changed      Campfire mode activated or deactivated
  retcon_applied        Admin rolled back a hallucinated action
  node_promoted         Latency auto-routing promoted a new local storyteller
  error                 Pipeline error (non-fatal events surfaced to operator)

Connection Model
----------------
Each WebSocket client gets its own asyncio.Queue (max 512 events).
Slow clients that fill their queue are silently dropped to avoid
backpressure blocking the pipeline.

New connections receive the last 200 events replayed immediately
so operators see recent history without waiting for new turns.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_REPLAY_BUFFER_SIZE = 200
_CLIENT_QUEUE_MAX   = 512


class TelemetryService:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._replay:  deque[dict]        = deque(maxlen=_REPLAY_BUFFER_SIZE)

    # ── Emit ──────────────────────────────────────────────────────────────────

    async def emit(self, event_type: str, **data: Any) -> None:
        """Broadcast a named event to all connected admin clients."""
        payload: dict[str, Any] = {
            "type": event_type,
            "ts":   datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self._replay.append(payload)

        dead: set[asyncio.Queue] = set()
        for q in self._clients:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.add(q)
        self._clients -= dead

        if dead:
            logger.debug(
                "Telemetry: dropped %d slow client(s) whose queue was full.", len(dead)
            )

    # ── Connection Management ─────────────────────────────────────────────────

    async def connect(self, ws: WebSocket) -> asyncio.Queue:
        """
        Accept a new WebSocket connection.

        Replays the last N events so the operator sees recent history,
        then adds the client to the live broadcast set.
        Returns the client's asyncio.Queue so the caller can forward events.
        """
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)

        # Replay recent events
        for event in self._replay:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                break

        self._clients.add(q)
        logger.info(
            "Telemetry: client connected (%d total).", len(self._clients)
        )
        return q

    def disconnect(self, q: asyncio.Queue) -> None:
        """Remove a client queue on WebSocket close."""
        self._clients.discard(q)
        logger.info(
            "Telemetry: client disconnected (%d remaining).", len(self._clients)
        )

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def client_count(self) -> int:
        """Number of currently connected WebSocket clients."""
        return len(self._clients)

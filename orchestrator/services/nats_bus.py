"""
NATS Message Bus — Multi-Agent Pub/Sub Client
=============================================
Wraps the `nats-py` client to provide typed publish/subscribe helpers for the
GM ↔ NPC ↔ map-renderer message bus defined in TDR §3.

Subject conventions
-------------------
  map.update.<campaign_id>    CoordinateUpdatePayload JSON
  map.reveal.<campaign_id>    FogRevealPayload JSON
  map.reset.<campaign_id>     (empty body)
  npc.<campaign_id>.<npc_id>  NPC sync payloads (future)
  gm.<campaign_id>            GM broadcast payloads (future)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.subscription import Subscription

from orchestrator.config import Settings

logger = logging.getLogger(__name__)


class NATSBus:
    """Lightweight async wrapper around the NATS client."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nc: NATSClient | None = None
        self._subscriptions: list[Subscription] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        url = f"nats://{self._settings.nats_host}:{self._settings.nats_port}"
        self._nc = await nats.connect(
            url,
            name="aetheris-scribe",
            reconnect_time_wait=2,
            max_reconnect_attempts=10,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnect,
            reconnected_cb=self._on_reconnect,
        )
        logger.info("NATS connection established: %s", url)

    async def disconnect(self) -> None:
        if self._nc:
            await self._nc.drain()
            logger.info("NATS connection drained and closed.")
            self._nc = None

    # ── Core publish / subscribe ──────────────────────────────────────────────

    async def publish(self, subject: str, payload: dict[str, Any] | None = None) -> None:
        """Serialise *payload* as JSON and publish to *subject*."""
        if self._nc is None:
            raise RuntimeError("NATSBus not connected.")
        data = json.dumps(payload or {}).encode()
        await self._nc.publish(subject, data)

    async def subscribe(
        self,
        subject: str,
        cb: Callable[[nats.aio.msg.Msg], Coroutine[Any, Any, None]],
    ) -> Subscription:
        """Subscribe to *subject* with an async callback."""
        if self._nc is None:
            raise RuntimeError("NATSBus not connected.")
        sub = await self._nc.subscribe(subject, cb=cb)
        self._subscriptions.append(sub)
        logger.debug("NATS subscribed to %s", subject)
        return sub

    # ── Typed map helpers ─────────────────────────────────────────────────────

    async def publish_coordinate_update(
        self,
        campaign_id: str,
        player_id: str,
        x: int,
        y: int,
        token: str = "",
        reveal_radius: int = 3,
    ) -> None:
        """Publish a player-coordinate update to the map renderer."""
        await self.publish(
            f"map.update.{campaign_id}",
            {
                "player_id":     player_id,
                "x":             x,
                "y":             y,
                "token":         token,
                "reveal_radius": reveal_radius,
            },
        )

    async def publish_fog_reveal(
        self,
        campaign_id: str,
        cells: list[int],
    ) -> None:
        """Reveal an explicit set of grid-cell indices for a campaign."""
        await self.publish(f"map.reveal.{campaign_id}", {"cells": cells})

    async def publish_map_reset(self, campaign_id: str) -> None:
        """Reset the Fog-of-War and all token positions for a campaign."""
        await self.publish(f"map.reset.{campaign_id}")

    # ── NATS event callbacks ──────────────────────────────────────────────────

    async def _on_error(self, e: Exception) -> None:
        logger.error("NATS error: %s", e)

    async def _on_disconnect(self) -> None:
        logger.warning("NATS disconnected.")

    async def _on_reconnect(self) -> None:
        logger.info("NATS reconnected.")

"""NATS JetStream message bus — multi-agent inter-process pub/sub.

Design notes
------------
* Connection is optional at startup — if NATS is unreachable the service
  degrades gracefully (publishes are no-ops, subscriptions are skipped).
* JetStream stream ``AETHERIS`` is created automatically on first connect.
* Epistemic boundaries: ``publish_scene_state`` accepts a ``visible_to`` list;
  only subjects for NPCs in that set receive the NpcReactionEvent fan-out.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import nats
import nats.errors
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from nats.js.api import StreamConfig

from orchestrator.config import Settings
from orchestrator.schemas.nats_schemas import (
    CombatBoardEvent,
    FogUpdateEvent,
    NpcReactionEvent,
    SceneStateEvent,
)

logger = logging.getLogger(__name__)

_STREAM_NAME = "AETHERIS"
_STREAM_SUBJECTS = ["aetheris.>"]


class NatsBus:
    """Thin async wrapper around the nats-py client with JetStream helpers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._connected: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to NATS and ensure the AETHERIS JetStream stream exists."""
        try:
            self._nc = await nats.connect(
                servers=[self._settings.nats_url],
                connect_timeout=5,
                reconnect_time_wait=2,
                max_reconnect_attempts=3,
                error_cb=self._on_error,
                disconnected_cb=self._on_disconnect,
                reconnected_cb=self._on_reconnect,
            )
            if self._settings.nats_jetstream_enabled:
                self._js = self._nc.jetstream()
                await self._ensure_stream()
            self._connected = True
            logger.info("NATS connection established (%s).", self._settings.nats_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NATS unavailable — multi-agent pub/sub disabled. (%s: %s)",
                type(exc).__name__,
                exc,
            )
            self._connected = False

    async def disconnect(self) -> None:
        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
            logger.info("NATS connection closed.")
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Raw publish / subscribe ───────────────────────────────────────────────

    async def publish(self, subject: str, payload: dict[str, Any]) -> None:
        """Publish a JSON payload to *subject*. No-op if not connected."""
        if not self._connected or self._nc is None:
            return
        data = json.dumps(payload).encode()
        if self._js and self._settings.nats_jetstream_enabled:
            await self._js.publish(subject, data)
        else:
            await self._nc.publish(subject, data)

    async def subscribe(
        self,
        subject: str,
        cb: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        durable: str | None = None,
    ) -> None:
        """Subscribe to *subject*, calling *cb* with the decoded JSON payload.

        When *durable* is set and JetStream is enabled the subscription survives
        reconnects and replays missed messages.
        """
        if not self._connected or self._nc is None:
            return

        async def _handler(msg: Any) -> None:
            try:
                payload = json.loads(msg.data.decode())
                await cb(payload)
                if hasattr(msg, "ack"):
                    await msg.ack()
            except Exception:  # noqa: BLE001
                logger.exception("Error in NATS subscriber for subject %s", subject)

        if self._js and durable and self._settings.nats_jetstream_enabled:
            await self._js.subscribe(subject, cb=_handler, durable=durable)
        else:
            await self._nc.subscribe(subject, cb=_handler)

    # ── Domain helpers ────────────────────────────────────────────────────────

    async def publish_scene_state(
        self,
        event: SceneStateEvent,
        npc_ids: list[str] | None = None,
    ) -> None:
        """Broadcast scene state and optionally fan out NpcReactionEvents.

        Args:
            event: The scene state to publish.
            npc_ids: If supplied, a NpcReactionEvent is published for each NPC
                whose ID appears in both *npc_ids* and ``event.visible_to``
                (or all of *npc_ids* when ``visible_to`` is None).
        """
        subject = f"aetheris.scene.{event.campaign_id}"
        await self.publish(subject, event.model_dump())

        if npc_ids:
            allowed = set(event.visible_to) if event.visible_to is not None else set(npc_ids)
            for npc_id in npc_ids:
                if npc_id not in allowed:
                    continue
                reaction = NpcReactionEvent(
                    npc_id=npc_id,
                    campaign_id=event.campaign_id,
                    turn_id=event.turn_id,
                    scene_summary=event.narrative_summary,
                )
                await self.publish(
                    f"aetheris.npc.{npc_id}.react",
                    reaction.model_dump(),
                )

    async def publish_combat_board(
        self,
        event: CombatBoardEvent,
    ) -> None:
        """Broadcast combat board state for simultaneous NPC resolution."""
        await self.publish(f"aetheris.combat.{event.campaign_id}", event.model_dump())

    async def publish_fog_update(
        self,
        event: FogUpdateEvent,
    ) -> None:
        """Publish a fog-of-war tile delta to the map renderer."""
        await self.publish(f"aetheris.fog.{event.campaign_id}", event.model_dump())

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _ensure_stream(self) -> None:
        assert self._js is not None
        try:
            await self._js.find_stream(name=_STREAM_NAME)
        except nats.errors.NotFoundError:
            await self._js.add_stream(
                StreamConfig(
                    name=_STREAM_NAME,
                    subjects=_STREAM_SUBJECTS,
                    max_msgs=100_000,
                    max_age=86_400,  # 24 h retention
                )
            )
            logger.info("JetStream stream '%s' created.", _STREAM_NAME)

    async def _on_error(self, exc: Exception) -> None:
        logger.warning("NATS error: %s", exc)

    async def _on_disconnect(self) -> None:
        logger.info("NATS disconnected.")
        self._connected = False

    async def _on_reconnect(self) -> None:
        logger.info("NATS reconnected.")
        self._connected = True

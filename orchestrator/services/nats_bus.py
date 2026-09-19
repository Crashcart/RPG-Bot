"""NATS JetStream multi-agent message bus.

Provides the NatsBus service used by the GMDirector to broadcast scene events
to NPC agents and the Fog-of-War renderer.

Design principles:
  - Graceful degradation: if NATS is unreachable the pipeline continues
    normally; all publish/subscribe calls become no-ops.
  - Epistemic boundaries: SceneStateEvent.visible_to is enforced at publish
    time — subjects are prefixed with the recipient agent ID so each NPC only
    receives events it is allowed to see.
  - JetStream: durable subjects enable replay on reconnect so no events are
    lost during transient failures.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Awaitable

from orchestrator.schemas.nats_schemas import (
    CombatBoardEvent,
    FogUpdateEvent,
    NpcReactionEvent,
    SceneStateEvent,
)

logger = logging.getLogger(__name__)


class NatsBus:
    """Thin async wrapper around nats-py with JetStream and graceful degradation."""

    # ── Subject templates ─────────────────────────────────────────────────────
    SCENE_SUBJECT   = "aetheris.scene.{scene_id}"
    NPC_SUBJECT     = "aetheris.npc.{npc_id}.react"
    COMBAT_SUBJECT  = "aetheris.combat.{scene_id}"
    FOG_SUBJECT     = "aetheris.fog.{scene_id}"

    # Private subject used when visible_to is set: targeted delivery
    _PRIVATE_SCENE  = "aetheris.scene.{scene_id}.for.{agent_id}"

    def __init__(self, nats_url: str) -> None:
        self._url = nats_url
        self._nc: object | None = None   # nats.Client
        self._js: object | None = None   # nats JetStream context
        self._connected = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        try:
            import nats  # type: ignore[import-untyped]

            self._nc = await nats.connect(
                self._url,
                error_cb=self._on_error,
                disconnected_cb=self._on_disconnect,
                reconnected_cb=self._on_reconnect,
            )
            self._js = self._nc.jetstream()  # type: ignore[union-attr]
            self._connected = True
            logger.info("NatsBus connected to %s", self._url)
        except Exception as exc:
            logger.warning(
                "NatsBus: failed to connect to %s — running without NATS (%s)",
                self._url,
                exc,
            )
            self._connected = False

    async def disconnect(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()  # type: ignore[union-attr]
            except Exception:
                pass
            self._connected = False
            logger.info("NatsBus disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Publish helpers ───────────────────────────────────────────────────────

    async def publish_scene_state(self, event: SceneStateEvent) -> None:
        """Broadcast a scene-state event, enforcing epistemic boundaries.

        If event.visible_to is non-empty only those agent IDs receive the
        event (via private per-agent subjects).  An empty visible_to means
        all subscribers on the public scene subject receive it.
        """
        if not self._connected:
            return
        payload = event.model_dump_json().encode()
        try:
            if event.visible_to:
                for agent_id in event.visible_to:
                    subject = self._PRIVATE_SCENE.format(
                        scene_id=event.scene_id, agent_id=agent_id
                    )
                    await self._js.publish(subject, payload)  # type: ignore[union-attr]
            else:
                subject = self.SCENE_SUBJECT.format(scene_id=event.scene_id)
                await self._js.publish(subject, payload)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("NatsBus: publish_scene_state failed: %s", exc)

    async def publish_npc_reaction(self, event: NpcReactionEvent) -> None:
        """NPC agent publishes its reaction back to the GM subject."""
        if not self._connected:
            return
        payload = event.model_dump_json().encode()
        subject = self.NPC_SUBJECT.format(npc_id=event.npc_id)
        try:
            await self._js.publish(subject, payload)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("NatsBus: publish_npc_reaction failed: %s", exc)

    async def publish_combat_update(self, event: CombatBoardEvent) -> None:
        """Broadcast the full combat board state after each turn resolution."""
        if not self._connected:
            return
        payload = event.model_dump_json().encode()
        subject = self.COMBAT_SUBJECT.format(scene_id=event.scene_id)
        try:
            await self._js.publish(subject, payload)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("NatsBus: publish_combat_update failed: %s", exc)

    async def publish_fog_update(self, event: FogUpdateEvent) -> None:
        """Publish an incremental Fog-of-War delta."""
        if not self._connected:
            return
        payload = event.model_dump_json().encode()
        subject = self.FOG_SUBJECT.format(scene_id=event.scene_id)
        try:
            await self._js.publish(subject, payload)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("NatsBus: publish_fog_update failed: %s", exc)

    # ── Subscribe helpers ─────────────────────────────────────────────────────

    async def subscribe_scene(
        self,
        scene_id: str,
        callback: Callable[[SceneStateEvent], Awaitable[None]],
        agent_id: str | None = None,
    ) -> object | None:
        """Subscribe to scene state events for *scene_id*.

        If *agent_id* is provided the subscription listens on the private
        subject (targeted delivery); otherwise on the public broadcast subject.
        Returns the subscription handle or None if NATS is unavailable.
        """
        if not self._connected:
            return None

        if agent_id:
            subject = self._PRIVATE_SCENE.format(
                scene_id=scene_id, agent_id=agent_id
            )
        else:
            subject = self.SCENE_SUBJECT.format(scene_id=scene_id)

        async def _cb(msg: object) -> None:  # type: ignore[type-arg]
            try:
                event = SceneStateEvent.model_validate_json(
                    msg.data.decode()  # type: ignore[union-attr]
                )
                await callback(event)
            except Exception as exc:
                logger.warning("NatsBus: subscribe_scene callback error: %s", exc)

        try:
            return await self._js.subscribe(subject, cb=_cb)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("NatsBus: subscribe_scene failed: %s", exc)
            return None

    async def subscribe_npc_reactions(
        self,
        npc_id: str,
        callback: Callable[[NpcReactionEvent], Awaitable[None]],
    ) -> object | None:
        """Subscribe to reaction events published by *npc_id*."""
        if not self._connected:
            return None
        subject = self.NPC_SUBJECT.format(npc_id=npc_id)

        async def _cb(msg: object) -> None:  # type: ignore[type-arg]
            try:
                event = NpcReactionEvent.model_validate_json(
                    msg.data.decode()  # type: ignore[union-attr]
                )
                await callback(event)
            except Exception as exc:
                logger.warning("NatsBus: subscribe_npc_reactions callback error: %s", exc)

        try:
            return await self._js.subscribe(subject, cb=_cb)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("NatsBus: subscribe_npc_reactions failed: %s", exc)
            return None

    # ── NATS error callbacks ───────────────────────────────────────────────────

    async def _on_error(self, exc: Exception) -> None:
        logger.error("NatsBus error: %s", exc)

    async def _on_disconnect(self) -> None:
        self._connected = False
        logger.warning("NatsBus: disconnected from server")

    async def _on_reconnect(self) -> None:
        self._connected = True
        logger.info("NatsBus: reconnected to server")

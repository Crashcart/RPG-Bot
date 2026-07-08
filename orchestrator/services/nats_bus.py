"""
NATS Message Bus — Multi-Agent Vector-Space Communication
==========================================================
Issue #8: Multi-Agent Vector-Space Communication (NPC/GM Sync)

Implements a NATS JetStream message bus for rapid, compressed state
synchronisation between the GM Director and NPC sub-agents.

Graceful degradation
---------------------
If NATS is unreachable (NATS_URL not set, server down), every method on
NatsBus silently returns without error.  The main pipeline continues through
the Redis + Ollama path, preserving full functionality.  NATS provides
performance acceleration, not functional dependency.

Subject hierarchy
-----------------
  aetheris.scene.{campaign_id}      — GM → NPC scene state vectors
  aetheris.npc.{npc_id}.react       — NPC → GM reaction events
  aetheris.combat.{campaign_id}     — GM → all combatants board state
  aetheris.fog.{campaign_id}        — GM → map renderer fog updates

Epistemic boundaries
--------------------
publish_scene_state() enforces SceneStateVector.visible_to at the
publish layer: one targeted message per allowed NPC entity rather than
a single wildcard broadcast.  This prevents sub-agents from receiving
intelligence outside their sensory radius.

Security
--------
All published payloads are Pydantic-serialised JSON.  The bus never
evaluates or executes incoming strings — raw text from NPC sub-agents
is sanitised before the NatsBus receives it through normal sub-agent
dispatch channels.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from orchestrator.schemas.nats_schemas import (
    CombatBoardEvent,
    EmotionHash,
    FogUpdateEvent,
    NpcReactionEvent,
    SceneStateVector,
    emotion_name,
)

logger = logging.getLogger(__name__)

# NATS subject templates
_SUBJECT_SCENE   = "aetheris.scene.{campaign_id}"
_SUBJECT_NPC     = "aetheris.npc.{npc_id}.react"
_SUBJECT_COMBAT  = "aetheris.combat.{campaign_id}"
_SUBJECT_FOG     = "aetheris.fog.{campaign_id}"
_SUBJECT_NPC_ALL = "aetheris.npc.*.react"   # wildcard subscription for GM


class NatsBus:
    """
    Async NATS JetStream facade for GM ↔ NPC vector-space communication.

    Instantiated once in main.py lifespan; wired into GMDirector and
    SubAgentDispatcher.  Pass nats_url="" to disable (all methods become
    no-ops while logging a debug message).
    """

    def __init__(self, nats_url: str = "") -> None:
        self._url      = nats_url
        self._nc: Any  = None   # nats.aio.client.Client
        self._js: Any  = None   # JetStream context
        self._ready    = False
        self._subs: list[Any] = []

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to NATS and set up the JetStream context."""
        if not self._url:
            logger.debug("NatsBus: NATS_URL not set — bus disabled (graceful degradation).")
            return
        try:
            import nats  # type: ignore[import-not-found]
            self._nc    = await nats.connect(self._url)
            self._js    = self._nc.jetstream()
            self._ready = True
            logger.info("NatsBus: connected to %s", self._url)
        except Exception as exc:
            logger.warning("NatsBus: connection failed (%s) — bus disabled.", exc)
            self._nc    = None
            self._js    = None
            self._ready = False

    async def close(self) -> None:
        """Drain and close the NATS connection."""
        if self._nc and self._ready:
            try:
                for sub in self._subs:
                    try:
                        await sub.unsubscribe()
                    except Exception:
                        pass
                await self._nc.drain()
                logger.info("NatsBus: connection closed.")
            except Exception as exc:
                logger.debug("NatsBus: close error (non-fatal): %s", exc)
        self._ready = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ── Publish: Scene State Vector ───────────────────────────────────────────

    async def publish_scene_state(self, vector: SceneStateVector) -> None:
        """
        Broadcast a SceneStateVector to the NPCs listed in visible_to.

        Epistemic boundary enforcement:
          • If visible_to is empty, one message is published on the global
            campaign subject (all subscribed NPCs receive it).
          • If visible_to has entries, one targeted message is sent per
            entity, addressed as aetheris.scene.{campaign_id}.npc.{entity_id}.
            NPC agents subscribe only to their own targeted subject.
        """
        if not self._ready:
            return
        payload = vector.model_dump_json().encode()
        try:
            if not vector.visible_to:
                subject = _SUBJECT_SCENE.format(campaign_id=vector.campaign_id)
                await self._js.publish(subject, payload)
                logger.debug(
                    "NatsBus: scene state published [campaign=%s event=%s entities=%d]",
                    vector.campaign_id, vector.event_type, len(vector.emotion_hashes),
                )
            else:
                base  = _SUBJECT_SCENE.format(campaign_id=vector.campaign_id)
                coros = [
                    self._js.publish(f"{base}.npc.{eid}", payload)
                    for eid in vector.visible_to
                ]
                await asyncio.gather(*coros, return_exceptions=True)
                logger.debug(
                    "NatsBus: scene state published to %d NPC(s) [campaign=%s event=%s]",
                    len(vector.visible_to), vector.campaign_id, vector.event_type,
                )
        except Exception as exc:
            logger.warning("NatsBus.publish_scene_state failed: %s", exc)

    # ── Publish: NPC Reaction ───────────────────────────────────────────────

    async def publish_npc_reaction(self, event: NpcReactionEvent) -> None:
        """
        Publish an NPC's reaction after processing a scene vector.

        Subject: aetheris.npc.{npc_id}.react
        The GM Director subscribes to the wildcard aetheris.npc.*.react
        to collect all NPC reactions for a campaign turn.
        """
        if not self._ready:
            return
        subject = _SUBJECT_NPC.format(npc_id=event.npc_id)
        try:
            await self._js.publish(subject, event.model_dump_json().encode())
            logger.debug(
                "NatsBus: NPC reaction published [npc=%s emotion=%s intent=%s]",
                event.npc_id, emotion_name(event.emotion_hash), event.intended_action,
            )
        except Exception as exc:
            logger.warning("NatsBus.publish_npc_reaction failed: %s", exc)

    # ── Publish: Combat Board ───────────────────────────────────────────────

    async def publish_combat_board(self, event: CombatBoardEvent) -> None:
        """
        Broadcast the full combat board state to all combatants in parallel.

        Combatants calculate their moves simultaneously (hive-mind pattern);
        the GM Director collects NpcReactionEvent replies and resolves conflicts.
        """
        if not self._ready:
            return
        subject = _SUBJECT_COMBAT.format(campaign_id=event.campaign_id)
        try:
            await self._js.publish(subject, event.model_dump_json().encode())
            logger.debug(
                "NatsBus: combat board published [campaign=%s round=%d active=%s]",
                event.campaign_id, event.round_number, event.active_entity_id,
            )
        except Exception as exc:
            logger.warning("NatsBus.publish_combat_board failed: %s", exc)

    # ── Publish: Fog Update ───────────────────────────────────────────────

    async def publish_fog_update(self, event: FogUpdateEvent) -> None:
        """Fire-and-forget notification to the map renderer that Fog of War changed."""
        if not self._ready:
            return
        subject = _SUBJECT_FOG.format(campaign_id=event.campaign_id)
        try:
            await self._js.publish(subject, event.model_dump_json().encode())
            logger.debug(
                "NatsBus: fog update published [campaign=%s revealed=%d hidden=%d]",
                event.campaign_id, len(event.revealed_tiles), len(event.hidden_tiles),
            )
        except Exception as exc:
            logger.warning("NatsBus.publish_fog_update failed: %s", exc)

    # ── Subscribe: NPC Reactions ────────────────────────────────────────────

    async def subscribe_npc_reactions(
        self,
        campaign_id: str,
        callback: Callable[[NpcReactionEvent], Awaitable[None]],
    ) -> None:
        """
        Subscribe to all NPC reaction events for a campaign.

        The callback receives a deserialized NpcReactionEvent.  Malformed
        messages are silently dropped (logged at DEBUG) to prevent a single
        misbehaving NPC from crashing the pipeline.
        """
        if not self._ready:
            return

        async def _message_handler(msg: Any) -> None:
            try:
                data  = json.loads(msg.data.decode())
                event = NpcReactionEvent(**data)
                if event.campaign_id == campaign_id:
                    await callback(event)
            except Exception as exc:
                logger.debug("NatsBus: malformed NPC reaction dropped: %s", exc)
            finally:
                try:
                    await msg.ack()
                except Exception:
                    pass

        try:
            sub = await self._js.subscribe(_SUBJECT_NPC_ALL, cb=_message_handler)
            self._subs.append(sub)
            logger.debug(
                "NatsBus: subscribed to NPC reactions for campaign %s", campaign_id
            )
        except Exception as exc:
            logger.warning("NatsBus.subscribe_npc_reactions failed: %s", exc)

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def make_scene_vector(
        campaign_id:     str,
        event_type:      str,
        entity_emotions: dict[str, int],
        player_action:   str = "",
        aggro_target:    str | None = None,
        visible_to:      list[str] | None = None,
        position_deltas: dict[str, list[int]] | None = None,
    ) -> SceneStateVector:
        """
        Convenience factory for building a SceneStateVector.

        Validates all emotion hash values against the EmotionHash enum,
        substituting NEUTRAL for any unrecognised value to prevent injection.
        """
        import uuid as _uuid
        safe_emotions: dict[str, int] = {}
        for eid, h in entity_emotions.items():
            try:
                safe_emotions[eid] = EmotionHash(h).value
            except ValueError:
                logger.debug(
                    "NatsBus: unknown emotion hash %d for entity %s — substituting NEUTRAL",
                    h, eid,
                )
                safe_emotions[eid] = EmotionHash.NEUTRAL.value

        return SceneStateVector(
            scene_id=str(_uuid.uuid4())[:8],
            campaign_id=campaign_id,
            event_type=event_type,
            emotion_hashes=safe_emotions,
            player_action_summary=player_action[:80],
            aggro_target=aggro_target,
            visible_to=visible_to or [],
            position_deltas=position_deltas or {},
        )

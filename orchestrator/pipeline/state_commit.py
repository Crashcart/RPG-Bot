"""
Phase 3 – State Commitment
===========================
Atomically applies the mechanical delta to PostgreSQL.
Publishes commit events to Redis for the CSV sync worker.

Two event types are published:
  • state_commit  — character stat/status/inventory changes
  • vehicle_commit — hull and subsystem mutations for assets
"""

from __future__ import annotations

import json
import logging

from orchestrator.schemas.payloads import (
    CharacterStatus,
    OllamaResolutionPayload,
    StateCommitPayload,
)
from orchestrator.services.cache import CacheService
from orchestrator.services.database import DatabaseService

logger = logging.getLogger(__name__)

_CSV_SYNC_CHANNEL = "csv_sync_events"


class StateCommitPhase:
    def __init__(self, db: DatabaseService, cache: CacheService) -> None:
        self._db    = db
        self._cache = cache

    async def commit(self, resolution: OllamaResolutionPayload) -> StateCommitPayload:
        """
        1. Fetch pre-commit character state.
        2. Atomically apply the full delta in PostgreSQL
           (stats + inventory + vehicle subsystems in one transaction).
        3. If character died, mark status = DEAD immediately.
        4. Publish commit events to Redis:
           - state_commit  → triggers character CSV regeneration
           - vehicle_commit → triggers asset_[vehicleId].csv regeneration
        """
        delta        = resolution.state_delta
        character_id = delta.character_id

        # Pre-state snapshot
        character = await self._db.get_character_by_id(character_id)
        if not character:
            raise ValueError(f"Character {character_id} not found for state commit.")

        pre_state = dict(character.stats)

        # Detect lethal outcome: explicit status_change or HP → 0
        lethal = (delta.status_change == CharacterStatus.DEAD)
        if not lethal:
            for sd in delta.stat_deltas:
                if sd.stat_key in ("hp", "hit_points", "health") and sd.new_value <= 0:
                    lethal = True
                    delta.status_change = CharacterStatus.DEAD
                    break

        # Atomic DB write (character + inventory + vehicles in one transaction)
        post_state = await self._db.apply_state_delta(delta)

        commit = StateCommitPayload(
            intent_id=resolution.intent_id,
            character_id=character_id,
            pre_state=pre_state,
            post_state=post_state,
            status_change=delta.status_change,
            lethal=lethal,
        )

        # ── Redis: character commit event ─────────────────────────────────────
        await self._cache.client.publish(
            _CSV_SYNC_CHANNEL,
            json.dumps({
                "event":         "state_commit",
                "character_id":  character_id,
                "lethal":        lethal,
                "status_change": delta.status_change.value if delta.status_change else None,
            }),
        )

        # ── Redis: vehicle commit events (one per vehicle changed) ────────────
        for vd in delta.vehicle_deltas:
            if not vd.vehicle_id:
                continue
            await self._cache.client.publish(
                _CSV_SYNC_CHANNEL,
                json.dumps({
                    "event":      "vehicle_commit",
                    "vehicle_id": vd.vehicle_id,
                    "hull_delta": vd.hull_delta,
                }),
            )
            logger.info(
                "Phase 3: vehicle_commit published for vehicle=%s hull_delta=%d",
                vd.vehicle_id,
                vd.hull_delta,
            )

        logger.info(
            "Phase 3 complete: character=%s lethal=%s vehicles_affected=%d",
            character_id,
            lethal,
            len(delta.vehicle_deltas),
        )

        return commit

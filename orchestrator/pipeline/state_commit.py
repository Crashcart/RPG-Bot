"""
Phase 3 – State Commitment
===========================
Atomically applies the mechanical delta to PostgreSQL.
Publishes a StateCommitPayload to Redis for the CSV sync worker.
If the delta results in character death, status is set to DEAD immediately.
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
        2. Atomically apply the delta in PostgreSQL.
        3. If character died, mark status = DEAD immediately.
        4. Publish commit event to Redis for the CSV sync worker.
        """
        delta = resolution.state_delta
        character_id = delta.character_id

        # Pre-state snapshot
        character = await self._db.get_character_by_id(character_id)
        if not character:
            raise ValueError(f"Character {character_id} not found for state commit.")

        pre_state = dict(character.stats)

        # Detect lethal outcome: status_change == DEAD or HP drops to zero
        lethal = (delta.status_change == CharacterStatus.DEAD)
        if not lethal:
            for sd in delta.stat_deltas:
                if sd.stat_key in ("hp", "hit_points", "health") and sd.new_value <= 0:
                    lethal = True
                    delta.status_change = CharacterStatus.DEAD
                    break

        # Atomic DB write
        post_state = await self._db.apply_state_delta(delta)

        commit = StateCommitPayload(
            intent_id=resolution.intent_id,
            character_id=character_id,
            pre_state=pre_state,
            post_state=post_state,
            status_change=delta.status_change,
            lethal=lethal,
        )

        # Notify CSV sync worker via Redis pub/sub
        await self._cache.client.publish(
            _CSV_SYNC_CHANNEL,
            json.dumps({
                "event":         "state_commit",
                "character_id":  character_id,
                "lethal":        lethal,
                "status_change": delta.status_change.value if delta.status_change else None,
            }),
        )

        logger.info(
            "Phase 3 complete: character=%s lethal=%s status=%s",
            character_id,
            lethal,
            delta.status_change,
        )

        return commit

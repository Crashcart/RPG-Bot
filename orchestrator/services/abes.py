"""
Ironclad GM – Autonomous Background Entity Simulation (ABES)
=============================================================
Advances NPCs, factions, and other entities while players are offline.
Operates entirely through pure mathematical/dice resolution — the LLM
narration pipeline is never invoked here.

Architecture
------------
1. World Tick: ``tick_all_campaigns()`` is called by the background loop
   (default every hour).  It sweeps the ``npc_entities`` table for entities
   whose ``next_tick_at`` is in the past and resolves each one via
   ``_resolve_entity()``.

2. Low-Fidelity Resolution: Python's ``random`` module rolls virtual dice for
   each entity and updates coordinates, HP, and inventory mathematically.
   Results are written back to ``npc_entities``.  No Ollama calls.

3. Event Logging: Significant events are appended to the ``world_delta``
   table.  The RAG catch-up engine reads this table when a player reconnects
   and translates entries into in-character rumours.

4. Discord Webhook Push: ``critical`` significance events trigger an HTTP
   POST to the campaign's configured Discord webhook, giving offline players
   an asynchronous push notification.

5. Time Dilation: The ``abes_time_dilation_factor`` setting scales how much
   in-game progress is applied per real-world tick interval.  A factor >1.0
   fast-forwards the simulation; <1.0 slows it down.

6. Offline Orders: Players can submit orders for their characters/companions
   via ``submit_offline_order()``.  The order is stored in ``downtime_tasks``
   (reuses the existing schema) and resolved on the next tick that covers the
   character — results land in ``world_delta`` as a personal narrative fragment.

7. Event-Driven Interrupts (Option 3): When a ``critical`` event is generated
   for an entity (e.g. home base attacked), ``flagged = TRUE`` is written on
   the ``world_delta`` row and the entity's ``active`` flag is set to ``FALSE``
   until the player manually resolves the situation.  This prevents passive
   loss of player assets.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx

from orchestrator.config import Settings
from orchestrator.schemas.payloads import (
    AbsTickResult,
    NpcEntityRequest,
    NpcEntityStatus,
    NpcIntentType,
    OfflineOrderRequest,
    OfflineOrderStatus,
    WorldDeltaEntry,
    WorldDeltaSignificance,
)

logger = logging.getLogger(__name__)

# ── Intent resolution parameters ─────────────────────────────────────────────
# Each intent type maps to a tuple: (progress_per_tick_pct, encounter_chance,
# success_threshold_d20, event_on_complete)
_INTENT_CONFIG: dict[str, dict[str, Any]] = {
    NpcIntentType.IDLE:    {"progress": 0,    "encounter": 0.02, "d20_threshold": 0,  "complete_event": "entity_idle"},
    NpcIntentType.TRAVEL:  {"progress": 0.20, "encounter": 0.10, "d20_threshold": 8,  "complete_event": "entity_arrived"},
    NpcIntentType.TRADE:   {"progress": 0.25, "encounter": 0.05, "d20_threshold": 10, "complete_event": "trade_complete"},
    NpcIntentType.CRAFT:   {"progress": 0.20, "encounter": 0.02, "d20_threshold": 12, "complete_event": "item_crafted"},
    NpcIntentType.FORAGE:  {"progress": 0.30, "encounter": 0.12, "d20_threshold": 10, "complete_event": "resources_gathered"},
    NpcIntentType.PATROL:  {"progress": 0.15, "encounter": 0.15, "d20_threshold": 12, "complete_event": "patrol_complete"},
    NpcIntentType.SIEGE:   {"progress": 0.10, "encounter": 0.25, "d20_threshold": 14, "complete_event": "siege_resolved"},
    NpcIntentType.RECRUIT: {"progress": 0.15, "encounter": 0.05, "d20_threshold": 11, "complete_event": "recruit_complete"},
    NpcIntentType.REST:    {"progress": 0.25, "encounter": 0.03, "d20_threshold": 5,  "complete_event": "rest_complete"},
    NpcIntentType.CUSTOM:  {"progress": 0.20, "encounter": 0.08, "d20_threshold": 10, "complete_event": "custom_complete"},
}

# Maximum world-delta entries returned by get_world_delta()
_MAX_DELTA_ROWS = 50


class AbsService:
    """
    Autonomous Background Entity Simulation service.

    Initialise once (in main.py lifespan) and call ``tick_all_campaigns()``
    from the background loop.
    """

    def __init__(self, settings: Settings, pool) -> None:
        self._pool           = pool
        self._webhook_url    = settings.abes_webhook_url
        self._tick_interval  = settings.abes_tick_interval_seconds

    # =========================================================================
    # Public API — called from main.py endpoints
    # =========================================================================

    async def register_entity(self, req: NpcEntityRequest) -> NpcEntityStatus:
        """
        Create or update an NPC entity in the simulation.
        Returns the entity's current status.
        """
        row = await self._pool.fetchrow(
            """
            INSERT INTO npc_entities
                (campaign_id, name, entity_type, current_location, destination,
                 intent_type, intent_description, stats, tick_interval_hours)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            ON CONFLICT DO NOTHING
            RETURNING id, name, entity_type, current_location, destination,
                      intent_type, intent_description, stats, active,
                      tick_interval_hours, last_ticked_at, next_tick_at
            """,
            UUID(req.campaign_id),
            req.name,
            req.entity_type.value,
            req.current_location,
            req.destination,
            req.intent_type.value,
            req.intent_description,
            json.dumps(req.stats),
            req.tick_interval_hours,
        )
        if not row:
            # ON CONFLICT — update instead
            row = await self._pool.fetchrow(
                """
                UPDATE npc_entities
                SET current_location    = $2,
                    destination         = $3,
                    intent_type         = $4,
                    intent_description  = $5,
                    stats               = $6::jsonb,
                    tick_interval_hours = $7,
                    active              = TRUE
                WHERE campaign_id = $8 AND name = $1
                RETURNING id, name, entity_type, current_location, destination,
                          intent_type, intent_description, stats, active,
                          tick_interval_hours, last_ticked_at, next_tick_at
                """,
                req.name,
                req.current_location,
                req.destination,
                req.intent_type.value,
                req.intent_description,
                json.dumps(req.stats),
                req.tick_interval_hours,
                UUID(req.campaign_id),
            )

        return self._row_to_entity_status(row, req.campaign_id)

    async def get_entity(self, entity_id: str, campaign_id: str) -> NpcEntityStatus | None:
        """Fetch a single entity by ID."""
        row = await self._pool.fetchrow(
            """
            SELECT id, name, entity_type, current_location, destination,
                   intent_type, intent_description, stats, active,
                   tick_interval_hours, last_ticked_at, next_tick_at
            FROM npc_entities
            WHERE id = $1 AND campaign_id = $2
            """,
            UUID(entity_id),
            UUID(campaign_id),
        )
        if not row:
            return None
        return self._row_to_entity_status(row, campaign_id)

    async def list_entities(self, campaign_id: str) -> list[NpcEntityStatus]:
        """List all entities for a campaign."""
        rows = await self._pool.fetch(
            """
            SELECT id, name, entity_type, current_location, destination,
                   intent_type, intent_description, stats, active,
                   tick_interval_hours, last_ticked_at, next_tick_at
            FROM npc_entities
            WHERE campaign_id = $1
            ORDER BY name
            """,
            UUID(campaign_id),
        )
        return [self._row_to_entity_status(r, campaign_id) for r in rows]

    async def get_world_delta(
        self,
        campaign_id: str,
        limit:       int   = 20,
        since:       datetime | None = None,
        significance: str | None = None,
    ) -> list[WorldDeltaEntry]:
        """
        Fetch recent world-delta entries for a campaign.

        ``since`` filters to events after a given timestamp.
        ``significance`` filters to 'minor', 'major', or 'critical' only.
        Used by the RAG catch-up pass to build in-character rumours.
        """
        limit = min(limit, _MAX_DELTA_ROWS)
        query = """
            SELECT wd.id, wd.campaign_id, wd.entity_id, wd.event_type,
                   wd.summary, wd.mechanical_data, wd.significance, wd.flagged,
                   wd.occurred_at, wd.notified,
                   ne.name AS entity_name
            FROM world_delta wd
            LEFT JOIN npc_entities ne ON ne.id = wd.entity_id
            WHERE wd.campaign_id = $1
        """
        params: list[Any] = [UUID(campaign_id)]
        idx = 2
        if since:
            query += f" AND wd.occurred_at > ${idx}"
            params.append(since)
            idx += 1
        if significance:
            query += f" AND wd.significance = ${idx}"
            params.append(significance)
            idx += 1
        query += f" ORDER BY wd.occurred_at DESC LIMIT ${idx}"
        params.append(limit)

        rows = await self._pool.fetch(query, *params)
        return [self._row_to_delta_entry(r) for r in rows]

    async def submit_offline_order(
        self, req: OfflineOrderRequest
    ) -> OfflineOrderStatus:
        """
        Record a player's offline orders for their character or companion.
        The order is persisted in downtime_tasks and resolved on the next
        ABES tick that covers the player's campaign.
        """
        resolves_at = datetime.now(timezone.utc) + timedelta(hours=req.duration_hours)

        row = await self._pool.fetchrow(
            """
            INSERT INTO downtime_tasks
                (campaign_id, player_id, description, duration_hours,
                 resolves_at, status)
            VALUES ($1, $2, $3, $4, $5, 'pending')
            RETURNING id, status, submitted_at, resolves_at
            """,
            UUID(req.campaign_id),
            req.player_id,
            f"[ABES:{req.intent_type.value}] {req.order_description}",
            req.duration_hours,
            resolves_at,
        )
        logger.info(
            "ABES offline order submitted: player=%s campaign=%s order=%r",
            req.player_id, req.campaign_id, req.order_description[:60],
        )
        return OfflineOrderStatus(
            order_id=str(row["id"]),
            campaign_id=req.campaign_id,
            player_id=req.player_id,
            character_name=req.character_name,
            order_description=req.order_description,
            intent_type=req.intent_type.value,
            duration_hours=req.duration_hours,
            status=row["status"],
            submitted_at=row["submitted_at"],
            resolves_at=row["resolves_at"],
        )

    async def resolve_pending_offline_orders(self) -> int:
        """
        Resolve downtime_tasks that were submitted via submit_offline_order().
        Returns the count of orders resolved.
        Called from the ABES background tick loop.
        """
        rows = await self._pool.fetch(
            """
            SELECT dt.id, dt.player_id, dt.campaign_id,
                   dt.description, dt.duration_hours
            FROM downtime_tasks dt
            WHERE dt.status = 'pending'
              AND dt.resolves_at <= NOW()
              AND dt.description LIKE '[ABES:%'
            LIMIT 20
            """,
        )
        if not rows:
            return 0

        resolved = 0
        for row in rows:
            task_id = row["id"]
            await self._pool.execute(
                "UPDATE downtime_tasks SET status = 'resolving' WHERE id = $1",
                task_id,
            )
            try:
                # Extract intent type from description prefix e.g. "[ABES:craft] ..."
                raw_desc = row["description"]
                intent_str = "custom"
                order_text = raw_desc
                if raw_desc.startswith("[ABES:") and "]" in raw_desc:
                    bracket_end = raw_desc.index("]")
                    intent_str  = raw_desc[6:bracket_end]
                    order_text  = raw_desc[bracket_end + 2:]

                result_summary = self._resolve_offline_order_math(
                    intent_str, order_text, row["duration_hours"]
                )

                await self._pool.execute(
                    """
                    UPDATE downtime_tasks
                    SET status           = 'complete',
                        result_narrative = $1,
                        resolved_at      = NOW()
                    WHERE id = $2
                    """,
                    result_summary,
                    task_id,
                )

                # Write to world_delta so the GM can reference it on catch-up
                await self._write_world_delta(
                    campaign_id=str(row["campaign_id"]),
                    entity_id=None,
                    event_type="offline_order_complete",
                    summary=result_summary,
                    mechanical_data={
                        "player_id":    row["player_id"],
                        "intent_type":  intent_str,
                        "description":  order_text[:200],
                    },
                    significance=WorldDeltaSignificance.MINOR,
                )
                resolved += 1
            except Exception as exc:
                logger.error("ABES offline order %s resolution failed: %s", task_id, exc)
                await self._pool.execute(
                    "UPDATE downtime_tasks SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                    task_id,
                )
        if resolved:
            logger.info("ABES: resolved %d offline order(s)", resolved)
        return resolved

    # =========================================================================
    # World Tick — called from background loop
    # =========================================================================

    async def tick_all_campaigns(self) -> list[AbsTickResult]:
        """
        Main entry point for the background loop.
        Sweeps all active campaigns and advances due NPC entities.
        Returns a result summary per campaign ticked.
        """
        campaign_ids = await self._pool.fetch(
            "SELECT id FROM campaigns WHERE active = TRUE"
        )
        results: list[AbsTickResult] = []
        for row in campaign_ids:
            cid = str(row["id"])
            try:
                result = await self._tick_campaign(cid)
                if result.entities_ticked > 0 or result.events_generated > 0:
                    results.append(result)
            except Exception as exc:
                logger.error("ABES tick failed for campaign %s: %s", cid, exc)
        return results

    async def _tick_campaign(self, campaign_id: str) -> AbsTickResult:
        """Run one tick pass for a single campaign."""
        # Fetch entities whose next_tick_at has passed
        entities = await self._pool.fetch(
            """
            SELECT id, name, entity_type, current_location, destination,
                   intent_type, intent_description, stats,
                   tick_interval_hours
            FROM npc_entities
            WHERE campaign_id = $1
              AND active = TRUE
              AND next_tick_at <= NOW()
            LIMIT 100
            """,
            UUID(campaign_id),
        )

        result = AbsTickResult(campaign_id=campaign_id)

        for entity in entities:
            events = await self._resolve_entity(campaign_id, entity)
            result.entities_ticked  += 1
            result.events_generated += len(events)
            for ev in events:
                if ev.significance == WorldDeltaSignificance.CRITICAL:
                    result.critical_events += 1

        # Resolve offline orders whose time has come
        await self.resolve_pending_offline_orders()

        # Fire webhook notifications for unnotified critical events
        webhooks = await self._fire_pending_webhooks(campaign_id)
        result.webhooks_fired = webhooks

        return result

    # =========================================================================
    # Entity Resolution — low-fidelity dice math
    # =========================================================================

    async def _resolve_entity(
        self, campaign_id: str, entity: Any
    ) -> list[WorldDeltaEntry]:
        """
        Advance one entity by one tick using pure dice resolution.
        Returns a list of WorldDeltaEntry objects written to the DB.
        """
        intent_type = entity["intent_type"]
        cfg         = _INTENT_CONFIG.get(intent_type, _INTENT_CONFIG[NpcIntentType.CUSTOM])

        stats: dict = dict(entity["stats"]) if entity["stats"] else {}
        events: list[WorldDeltaEntry] = []

        # ── Encounter Roll ────────────────────────────────────────────────────
        encounter_roll = random.random()
        if encounter_roll < cfg["encounter"]:
            encounter_event = await self._resolve_encounter(
                campaign_id, entity, stats, cfg
            )
            if encounter_event:
                events.append(encounter_event)

        # ── Progress Roll ─────────────────────────────────────────────────────
        if cfg["progress"] > 0:
            d20 = random.randint(1, 20)
            threshold = cfg["d20_threshold"]

            if d20 >= threshold:
                # Successful progress this tick
                current_progress = float(stats.get("intent_progress", 0.0))
                new_progress     = min(1.0, current_progress + cfg["progress"])
                stats["intent_progress"] = round(new_progress, 3)

                progress_event = await self._write_world_delta(
                    campaign_id  = campaign_id,
                    entity_id    = str(entity["id"]),
                    event_type   = "entity_progress",
                    summary      = (
                        f"{entity['name']} makes progress on their {intent_type} task "
                        f"({int(new_progress * 100)}% complete)."
                    ),
                    mechanical_data = {
                        "intent_type":     intent_type,
                        "progress":        new_progress,
                        "d20_roll":        d20,
                        "threshold":       threshold,
                    },
                    significance = WorldDeltaSignificance.MINOR,
                )
                events.append(progress_event)

                # ── Completion Check ──────────────────────────────────────────
                if new_progress >= 1.0:
                    complete_event = await self._resolve_completion(
                        campaign_id, entity, intent_type, cfg, stats
                    )
                    events.append(complete_event)
                    stats["intent_progress"] = 0.0
            else:
                # Setback — minor stat penalty if applicable
                if "hp" in stats and "max_hp" in stats:
                    setback_dmg         = random.randint(1, 4)
                    stats["hp"]         = max(0, int(stats["hp"]) - setback_dmg)
                    setback_event = await self._write_world_delta(
                        campaign_id  = campaign_id,
                        entity_id    = str(entity["id"]),
                        event_type   = "entity_setback",
                        summary      = (
                            f"{entity['name']} suffers a setback on their {intent_type} task "
                            f"(rolled {d20}, needed {threshold})."
                        ),
                        mechanical_data = {
                            "intent_type": intent_type,
                            "d20_roll":    d20,
                            "hp_lost":     setback_dmg,
                            "hp_remaining": stats["hp"],
                        },
                        significance = WorldDeltaSignificance.MINOR,
                    )
                    events.append(setback_event)

                    # Check for entity death
                    if stats["hp"] <= 0:
                        death_event = await self._resolve_death(
                            campaign_id, entity, stats
                        )
                        events.append(death_event)

        # ── Travel: update location string ───────────────────────────────────
        if intent_type == NpcIntentType.TRAVEL and entity["destination"]:
            progress = float(stats.get("intent_progress", 0.0))
            if 0 < progress < 1.0:
                # Partial travel description update
                stats["travel_note"] = (
                    f"En route from {entity['current_location'] or 'unknown'} "
                    f"to {entity['destination']} ({int(progress * 100)}%)."
                )

        # ── Persist updated stats + advance next_tick_at ─────────────────────
        interval_hrs = int(entity["tick_interval_hours"])
        next_tick    = datetime.now(timezone.utc) + timedelta(hours=interval_hrs)
        await self._pool.execute(
            """
            UPDATE npc_entities
            SET stats          = $1::jsonb,
                last_ticked_at = NOW(),
                next_tick_at   = $2
            WHERE id = $3
            """,
            json.dumps(stats),
            next_tick,
            entity["id"],
        )

        return events

    async def _resolve_encounter(
        self,
        campaign_id: str,
        entity:      Any,
        stats:       dict,
        cfg:         dict,
    ) -> WorldDeltaEntry | None:
        """Roll a random encounter for an entity on the move."""
        d20 = random.randint(1, 20)
        if d20 >= 15:
            return await self._write_world_delta(
                campaign_id = campaign_id,
                entity_id   = str(entity["id"]),
                event_type  = "entity_encounter",
                summary     = (
                    f"{entity['name']} had an unexpected encounter while "
                    f"on their {entity['intent_type']} task."
                ),
                mechanical_data = {"d20_roll": d20, "outcome": "survived"},
                significance    = WorldDeltaSignificance.MINOR,
            )
        return None

    async def _resolve_completion(
        self,
        campaign_id: str,
        entity:      Any,
        intent_type: str,
        cfg:         dict,
        stats:       dict,
    ) -> WorldDeltaEntry:
        """Handle intent completion — update location if travelling, etc."""
        new_location = entity["current_location"]
        if intent_type == NpcIntentType.TRAVEL and entity["destination"]:
            new_location = entity["destination"]
            await self._pool.execute(
                """
                UPDATE npc_entities
                SET current_location = $1, destination = '', intent_type = 'idle'
                WHERE id = $2
                """,
                new_location,
                entity["id"],
            )
            return await self._write_world_delta(
                campaign_id     = campaign_id,
                entity_id       = str(entity["id"]),
                event_type      = cfg["complete_event"],
                summary         = (
                    f"{entity['name']} has arrived at {new_location}."
                ),
                mechanical_data = {
                    "intent_type": intent_type,
                    "destination": new_location,
                },
                significance    = WorldDeltaSignificance.MAJOR,
            )
        else:
            return await self._write_world_delta(
                campaign_id     = campaign_id,
                entity_id       = str(entity["id"]),
                event_type      = cfg["complete_event"],
                summary         = (
                    f"{entity['name']} has completed their {intent_type} task."
                ),
                mechanical_data = {"intent_type": intent_type},
                significance    = WorldDeltaSignificance.MAJOR,
            )

    async def _resolve_death(
        self,
        campaign_id: str,
        entity:      Any,
        stats:       dict,
    ) -> WorldDeltaEntry:
        """
        Handle entity death — deactivate the entity and write a critical
        world-delta event.  Sets flagged=TRUE so the simulation pauses until
        a player or GM resolves the situation.
        """
        await self._pool.execute(
            "UPDATE npc_entities SET active = FALSE WHERE id = $1",
            entity["id"],
        )
        logger.info(
            "ABES: entity '%s' (%s) died in campaign %s",
            entity["name"], str(entity["id"]), campaign_id,
        )
        return await self._write_world_delta(
            campaign_id     = campaign_id,
            entity_id       = str(entity["id"]),
            event_type      = "entity_died",
            summary         = (
                f"{entity['name']} has perished. Their long-term task has ended."
            ),
            mechanical_data = {"hp": 0, "last_stats": stats},
            significance    = WorldDeltaSignificance.CRITICAL,
            flagged         = True,
        )

    # =========================================================================
    # Offline Order Math Resolution (no LLM)
    # =========================================================================

    @staticmethod
    def _resolve_offline_order_math(
        intent_type: str,
        description: str,
        duration_hours: int,
    ) -> str:
        """
        Produce a terse result summary for an offline order using pure dice
        math.  No LLM call — the GM catch-up layer handles narrative prose.
        """
        d20 = random.randint(1, 20)
        if d20 >= 18:
            outcome = "exceptional success"
        elif d20 >= 12:
            outcome = "success"
        elif d20 >= 7:
            outcome = "partial success"
        else:
            outcome = "complication"

        return (
            f"[ABES result — {outcome} (d20={d20})] "
            f"After {duration_hours}h of '{description[:80]}', "
            f"your character achieved a {outcome}."
        )

    # =========================================================================
    # World Delta helpers
    # =========================================================================

    async def _write_world_delta(
        self,
        campaign_id:     str,
        entity_id:       str | None,
        event_type:      str,
        summary:         str,
        mechanical_data: dict,
        significance:    WorldDeltaSignificance,
        flagged:         bool = False,
    ) -> WorldDeltaEntry:
        """Append one row to world_delta and return a model representation."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO world_delta
                (campaign_id, entity_id, event_type, summary,
                 mechanical_data, significance, flagged)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            RETURNING id, occurred_at, notified
            """,
            UUID(campaign_id),
            UUID(entity_id) if entity_id else None,
            event_type,
            summary,
            json.dumps(mechanical_data),
            significance.value,
            flagged,
        )
        return WorldDeltaEntry(
            delta_id        = str(row["id"]),
            campaign_id     = campaign_id,
            entity_id       = entity_id,
            event_type      = event_type,
            summary         = summary,
            mechanical_data = mechanical_data,
            significance    = significance,
            flagged         = flagged,
            occurred_at     = row["occurred_at"],
            notified        = row["notified"],
        )

    # =========================================================================
    # Discord Webhook Push (critical events)
    # =========================================================================

    async def _fire_pending_webhooks(self, campaign_id: str) -> int:
        """
        Send Discord webhook notifications for unnotified critical world-delta
        events.  Returns the number of webhooks fired successfully.
        """
        if not self._webhook_url:
            return 0

        rows = await self._pool.fetch(
            """
            SELECT id, summary, event_type, occurred_at
            FROM world_delta
            WHERE campaign_id = $1
              AND significance = 'critical'
              AND notified     = FALSE
            ORDER BY occurred_at
            LIMIT 10
            """,
            UUID(campaign_id),
        )
        if not rows:
            return 0

        fired = 0
        for row in rows:
            payload = {
                "embeds": [{
                    "title":       "⚠️ World Event",
                    "description": row["summary"],
                    "color":       0xDC143C,
                    "footer":      {"text": f"Event: {row['event_type']}"},
                    "timestamp":   row["occurred_at"].isoformat(),
                }]
            }
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(self._webhook_url, json=payload)
                    resp.raise_for_status()
                await self._pool.execute(
                    "UPDATE world_delta SET notified = TRUE WHERE id = $1",
                    row["id"],
                )
                fired += 1
            except Exception as exc:
                logger.warning(
                    "ABES webhook failed for delta %s: %s", str(row["id"]), exc
                )

        if fired:
            logger.info(
                "ABES: fired %d critical webhook notification(s) for campaign %s",
                fired, campaign_id,
            )
        return fired

    # =========================================================================
    # Internal helpers
    # =========================================================================

    @staticmethod
    def _row_to_entity_status(row: Any, campaign_id: str) -> NpcEntityStatus:
        stats = dict(row["stats"]) if row["stats"] else {}
        return NpcEntityStatus(
            entity_id           = str(row["id"]),
            campaign_id         = campaign_id,
            name                = row["name"],
            entity_type         = row["entity_type"],
            current_location    = row["current_location"],
            destination         = row["destination"],
            intent_type         = row["intent_type"],
            intent_description  = row["intent_description"],
            stats               = stats,
            active              = row["active"],
            tick_interval_hours = row["tick_interval_hours"],
            last_ticked_at      = row["last_ticked_at"],
            next_tick_at        = row["next_tick_at"],
        )

    @staticmethod
    def _row_to_delta_entry(row: Any) -> WorldDeltaEntry:
        mdata = dict(row["mechanical_data"]) if row["mechanical_data"] else {}
        return WorldDeltaEntry(
            delta_id        = str(row["id"]),
            campaign_id     = str(row["campaign_id"]),
            entity_id       = str(row["entity_id"]) if row["entity_id"] else None,
            entity_name     = row["entity_name"] if "entity_name" in row.keys() else None,
            event_type      = row["event_type"],
            summary         = row["summary"],
            mechanical_data = mdata,
            significance    = WorldDeltaSignificance(row["significance"]),
            flagged         = row["flagged"],
            occurred_at     = row["occurred_at"],
            notified        = row["notified"],
        )

"""
Ironclad GM – Autonomous Background Entity Simulation (ABES)
=============================================================
WorldTickService runs periodic world ticks for every enabled campaign.

On each tick the engine:
  1. Fetches all active NPC intents for the campaign.
  2. Rolls virtual dice (secrets.randbelow) — no LLM involved.
  3. Updates entity progress, HP, and status in npc_long_term_intents.
  4. Writes significant events to world_delta.
  5. Fires a Discord webhook embed for major / critical events (fails silently).

The GMDirector calls get_recent_events() to inject offline world-changes
into narrative prompts as organic in-character rumours when players reconnect.

Architecture rules obeyed:
  - LLM is NEVER called here; this is pure math / RNG.
  - secrets.randbelow provides true RNG for fair play.
  - All DB writes use asyncpg transactions so partial ticks cannot corrupt state.
  - Discord webhooks use httpx and suppress all exceptions.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)

# ── Dice helpers ──────────────────────────────────────────────────────────────

def _roll(sides: int) -> int:
    """Roll a fair die with `sides` faces using a CSPRNG."""
    return secrets.randbelow(sides) + 1


# ── Severity thresholds ───────────────────────────────────────────────────────

_HAZARD_THRESHOLD    = 4   # roll ≤ threshold → complication
_CRITICAL_THRESHOLD  = 1   # roll == 1 → critical / near-death complication
_DEATH_HP_THRESHOLD  = 0   # HP at or below 0 → entity dies


# ── WorldTickService ──────────────────────────────────────────────────────────

class WorldTickService:
    """
    Background asyncio service.  One instance per application; shared pool.

    Usage (in main.py lifespan):
        abes = WorldTickService(pool=db.pool)
        abes_task = asyncio.create_task(abes.run())
        ...
        abes_task.cancel()
    """

    def __init__(self, pool) -> None:
        self._pool = pool
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop.  Ticks each enabled campaign at its configured interval."""
        self._running = True
        logger.info("ABES WorldTickService started")

        while self._running:
            try:
                await self._tick_all_campaigns()
            except Exception as exc:
                logger.error("ABES tick sweep failed: %s", exc)

            # Default sleep; fine-grained interval is per-campaign.
            # We sweep every 60 s to catch campaigns with short intervals.
            await asyncio.sleep(60)

    def stop(self) -> None:
        self._running = False

    # ── Campaign sweep ────────────────────────────────────────────────────────

    async def _tick_all_campaigns(self) -> None:
        """Fetch every enabled campaign that is due for a tick and process it."""
        rows = await self._pool.fetch(
            """
            SELECT c.id AS campaign_id,
                   COALESCE(a.tick_interval_s, 3600)  AS tick_interval_s,
                   COALESCE(a.time_dilation,   1.0)   AS time_dilation,
                   COALESCE(a.webhook_url,     '')     AS webhook_url,
                   COALESCE(a.enabled,         TRUE)  AS enabled
            FROM campaigns c
            LEFT JOIN abes_config a ON a.campaign_id = c.id
            WHERE c.active = TRUE
              AND COALESCE(a.enabled, TRUE) = TRUE
            """
        )

        now = datetime.now(timezone.utc)

        for row in rows:
            campaign_id   = row["campaign_id"]
            interval_s    = row["tick_interval_s"]
            time_dilation = row["time_dilation"]
            webhook_url   = row["webhook_url"]

            # Check if enough real-world time has passed for this campaign's tick.
            last_tick = await self._get_last_tick_time(campaign_id)
            if last_tick is not None:
                elapsed_s = (now - last_tick).total_seconds()
                if elapsed_s < interval_s:
                    continue  # not yet time

            try:
                events = await self.tick_campaign(
                    campaign_id=campaign_id,
                    time_dilation=time_dilation,
                )
                logger.info(
                    "ABES tick: campaign=%s events=%d", campaign_id, len(events)
                )
                # Fire webhook for major / critical events
                if webhook_url:
                    major = [e for e in events if e["severity"] in ("major", "critical")]
                    for event in major:
                        await self._fire_webhook(webhook_url, event)
            except Exception as exc:
                logger.error("ABES tick failed for campaign %s: %s", campaign_id, exc)

    async def _get_last_tick_time(self, campaign_id: UUID) -> datetime | None:
        """Return the most recent world_delta timestamp for a campaign."""
        row = await self._pool.fetchrow(
            "SELECT MAX(occurred_at) AS t FROM world_delta WHERE campaign_id = $1",
            campaign_id,
        )
        return row["t"] if row and row["t"] else None

    # ── Core tick ─────────────────────────────────────────────────────────────

    async def tick_campaign(
        self,
        campaign_id: UUID,
        time_dilation: float = 1.0,
    ) -> list[dict[str, Any]]:
        """
        Process one world tick for the given campaign.

        Returns a list of event dicts (mirrors world_delta schema) for all events
        written during this tick.  Callers can use these for webhook payloads or
        test assertions without querying the DB again.
        """
        intents = await self._pool.fetch(
            """
            SELECT id, entity_name, entity_type, intent_type, description,
                   skill_rating, hp, max_hp, progress, destination, ticks_remaining
            FROM npc_long_term_intents
            WHERE campaign_id = $1 AND status = 'active'
            ORDER BY updated_at
            """,
            campaign_id,
        )

        events: list[dict[str, Any]] = []
        for intent in intents:
            evt = await self._process_npc_intent(
                campaign_id=campaign_id,
                intent=dict(intent),
                time_dilation=time_dilation,
            )
            if evt:
                events.append(evt)

        return events

    # ── NPC intent resolution ─────────────────────────────────────────────────

    async def _process_npc_intent(
        self,
        campaign_id: UUID,
        intent: dict[str, Any],
        time_dilation: float,
    ) -> dict[str, Any] | None:
        """
        Resolve one tick of progress for a single NPC intent.
        Pure Python + CSPRNG — no LLM calls.

        Returns an event dict if a world_delta row was written, else None.
        """
        intent_id    = intent["id"]
        entity_name  = intent["entity_name"]
        intent_type  = intent["intent_type"]
        skill_rating = intent["skill_rating"]
        hp           = intent["hp"]
        max_hp       = intent["max_hp"]
        progress     = intent["progress"]
        ticks_left   = intent["ticks_remaining"]

        # ── Dice resolution ───────────────────────────────────────────────────
        # Roll 1d20; high roll = smooth progress, low roll = complication.
        # Skill rating acts as a bonus (higher skill → harder to fail).
        roll = _roll(20)
        adjusted = roll + max(0, (skill_rating - 10) // 2)  # proficiency-style bonus

        event: dict[str, Any] | None = None

        if roll <= _CRITICAL_THRESHOLD:
            # Critical failure: heavy HP damage, progress stalls
            damage = _roll(6) + 2
            hp = max(0, hp - damage)
            severity = "critical"
            event_type = "complication"
            summary = (
                f"{entity_name} suffered a critical setback during '{intent_type}': "
                f"took {damage} damage (HP now {hp}/{max_hp})."
            )
            mechanical_data = {
                "roll": roll, "adjusted": adjusted,
                "damage": damage, "hp_before": intent["hp"], "hp_after": hp,
            }

        elif roll <= _HAZARD_THRESHOLD:
            # Minor complication: small HP damage, progress slows
            damage = _roll(4)
            hp = max(0, hp - damage)
            severity = "minor"
            event_type = "complication"
            summary = (
                f"{entity_name} hit a complication during '{intent_type}': "
                f"took {damage} damage (HP now {hp}/{max_hp})."
            )
            mechanical_data = {
                "roll": roll, "adjusted": adjusted,
                "damage": damage, "hp_before": intent["hp"], "hp_after": hp,
            }

        else:
            # Successful progress: advance toward goal
            # Scale progress gain by time_dilation
            gain = max(1, int((roll + adjusted) * time_dilation // 5))
            progress = min(100, progress + gain)
            severity = "minor"
            event_type = "progress"
            summary = (
                f"{entity_name} made progress on '{intent_type}' "
                f"(+{gain}%, now {progress}% complete)."
            )
            mechanical_data = {
                "roll": roll, "adjusted": adjusted,
                "gain": gain, "progress_before": intent["progress"], "progress_after": progress,
            }

        # ── Death check ───────────────────────────────────────────────────────
        new_status = "active"
        if hp <= _DEATH_HP_THRESHOLD:
            new_status = "failed"
            severity = "critical"
            event_type = "death"
            summary = (
                f"{entity_name} has died or been destroyed while attempting '{intent_type}'. "
                f"Their {intent_type} will not be completed."
            )
            mechanical_data["cause"] = "hp_depleted"

        # ── Task completion check ─────────────────────────────────────────────
        elif progress >= 100:
            new_status = "completed"
            severity = "major"
            event_type = "task_complete"
            summary = (
                f"{entity_name} has completed '{intent_type}'"
                + (f" — reached {intent['destination']}." if intent.get("destination") else ".")
            )
            mechanical_data["completed_at_roll"] = roll

        # ── Tick countdown ────────────────────────────────────────────────────
        new_ticks = None
        if ticks_left is not None:
            new_ticks = max(0, ticks_left - 1)
            if new_ticks == 0 and new_status == "active":
                new_status = "failed"
                event_type = "task_complete"
                severity = "minor"
                summary = (
                    f"{entity_name}'s '{intent_type}' task has expired without completion."
                )

        # ── Persist changes ───────────────────────────────────────────────────
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Update the NPC intent row
                await conn.execute(
                    """
                    UPDATE npc_long_term_intents
                    SET hp              = $1,
                        progress        = $2,
                        status          = $3,
                        ticks_remaining = $4,
                        updated_at      = NOW()
                    WHERE id = $5
                    """,
                    hp, progress, new_status, new_ticks, intent_id,
                )

                # Write world_delta row (always — even minor progress is logged)
                await conn.execute(
                    """
                    INSERT INTO world_delta
                        (campaign_id, entity_name, event_type, severity, summary, mechanical_data)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    """,
                    campaign_id,
                    entity_name,
                    event_type,
                    severity,
                    summary,
                    str(mechanical_data).replace("'", '"'),  # basic JSON serialization
                )

        import json
        return {
            "campaign_id":     str(campaign_id),
            "entity_name":     entity_name,
            "event_type":      event_type,
            "severity":        severity,
            "summary":         summary,
            "mechanical_data": mechanical_data,
            "new_status":      new_status,
        }

    # ── GMDirector catch-up ───────────────────────────────────────────────────

    async def get_recent_events(
        self,
        campaign_id: UUID,
        since: datetime | None = None,
        limit: int = 20,
        min_severity: str = "minor",
    ) -> list[dict[str, Any]]:
        """
        Return world_delta rows for a campaign, newest first.
        The GMDirector injects these into narrative prompts as in-character rumours.

        Args:
            since:        If set, only return events after this timestamp.
            limit:        Max rows returned (0 = no limit).
            min_severity: 'minor' | 'major' | 'critical' — filter by importance.
        """
        severity_order = {"minor": 0, "major": 1, "critical": 2}
        min_sev_value  = severity_order.get(min_severity, 0)

        # Build dynamic WHERE clause
        conditions = ["campaign_id = $1"]
        params: list[Any] = [campaign_id]

        if since is not None:
            params.append(since)
            conditions.append(f"occurred_at > ${len(params)}")

        if min_sev_value > 0:
            allowed_severities = [s for s, v in severity_order.items() if v >= min_sev_value]
            params.append(allowed_severities)
            conditions.append(f"severity = ANY(${len(params)})")

        where = " AND ".join(conditions)
        limit_clause = f"LIMIT {limit}" if limit > 0 else ""

        rows = await self._pool.fetch(
            f"""
            SELECT entity_name, event_type, severity, summary, mechanical_data, occurred_at
            FROM world_delta
            WHERE {where}
            ORDER BY occurred_at DESC
            {limit_clause}
            """,
            *params,
        )

        return [
            {
                "entity_name":     r["entity_name"],
                "event_type":      r["event_type"],
                "severity":        r["severity"],
                "summary":         r["summary"],
                "mechanical_data": dict(r["mechanical_data"]) if r["mechanical_data"] else {},
                "occurred_at":     r["occurred_at"].isoformat(),
            }
            for r in rows
        ]

    # ── NPC intent registration ───────────────────────────────────────────────

    async def upsert_npc_intent(
        self,
        campaign_id: UUID,
        entity_name: str,
        entity_type: str,
        intent_type: str,
        description: str,
        skill_rating: int = 10,
        hp: int = 10,
        destination: str = "",
        ticks_remaining: int | None = None,
    ) -> str:
        """
        Register or replace an NPC's active long-term goal.
        Replaces any previous 'active' intent of the same type for this entity.

        Returns the UUID of the upserted intent row.
        """
        # Deactivate previous active intents of the same type for this entity
        await self._pool.execute(
            """
            UPDATE npc_long_term_intents
            SET status = 'paused', updated_at = NOW()
            WHERE campaign_id = $1
              AND entity_name  = $2
              AND intent_type  = $3
              AND status       = 'active'
            """,
            campaign_id, entity_name, intent_type,
        )

        row = await self._pool.fetchrow(
            """
            INSERT INTO npc_long_term_intents
                (campaign_id, entity_name, entity_type, intent_type, description,
                 skill_rating, hp, max_hp, destination, ticks_remaining)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, $9)
            RETURNING id
            """,
            campaign_id,
            entity_name,
            entity_type,
            intent_type,
            description,
            skill_rating,
            hp,
            destination,
            ticks_remaining,
        )
        return str(row["id"])

    # ── Discord webhook ───────────────────────────────────────────────────────

    async def _fire_webhook(self, webhook_url: str, event: dict[str, Any]) -> None:
        """POST a Discord embed to the campaign's webhook URL.  Fails silently."""
        if not webhook_url:
            return

        colour = 0xFF0000 if event["severity"] == "critical" else 0xFF8C00
        payload = {
            "embeds": [{
                "title":       f"⚠️ World Event — {event['event_type'].replace('_', ' ').title()}",
                "description": event["summary"],
                "color":       colour,
                "footer":      {"text": f"Entity: {event['entity_name']} • Severity: {event['severity']}"},
            }]
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
        except Exception as exc:
            logger.debug("ABES webhook delivery failed: %s", exc)

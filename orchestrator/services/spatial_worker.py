"""
Spatial Worker — Asynchronous Cargo Hauling & Transit Engine
=============================================================
Background asyncio service that advances vehicles along plotted routes every
SPATIAL_TICK_INTERVAL_SECONDS seconds.  The LLM is never invoked during
transit — position math is deterministic Python.

Architecture
------------
1. SpatialWorker starts a single asyncio task (_run_loop).
2. Every tick it fetches all vehicles WHERE transit_state = 'in_transit'.
3. For each vehicle:
   a. Reads nav_computer JSONB to get current coords, destination, speed.
   b. Moves the vehicle one step along the route vector.
   c. Checks new position against hazard_zones (Euclidean distance).
   d. If inside a hazard zone → trigger interdiction (pause + Discord alert).
   e. If fuel exhausted → trigger fuel_empty event (emergency stop).
   f. If distance_remaining ≤ 0 → arrival event (Discord alert, state=idle).
4. All mutations write to vehicles.nav_computer + transit_log.

Discord notifications
---------------------
Set SPATIAL_DISCORD_WEBHOOK_URL in .env to receive real-time embeds for
arrivals, interdictions, and fuel emergencies.  Failures are silent.

Wiring (main.py lifespan)
---------
    from orchestrator.services.spatial_worker import SpatialWorker

    spatial_worker = SpatialWorker(pool=db.pool, settings=settings)
    await spatial_worker.start()
    ...
    await spatial_worker.stop()

Course plotting (example)
---------
    nav = NavComputerState(
        transit_state=TransitState.IN_TRANSIT,
        origin_name="Kepler Station",
        destination_name="Mining Colony Theta",
        origin_x=0.0, origin_y=0.0,
        dest_x=450.0, dest_y=120.0,
        current_x=0.0, current_y=0.0,
        speed=10.0, fuel_remaining=100.0, fuel_per_tick=0.5,
        departure_at=datetime.now(timezone.utc),
    )
    await spatial_worker.plot_course(vehicle_id, nav)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from orchestrator.schemas.payloads import (
    NavComputerState,
    TransitEvent,
    TransitEventType,
    TransitState,
)

if TYPE_CHECKING:
    import asyncpg
    from orchestrator.config import Settings

logger = logging.getLogger(__name__)

_FUEL_WARNING_THRESHOLD = 0.20   # warn when fuel drops below 20 %
_MAX_TICK_ERRORS        = 5      # stop processing a vehicle after N consecutive errors


class SpatialWorker:
    """
    Long-lived background service for spatial transit simulation.
    Call start() in the app lifespan; stop() on shutdown.
    """

    def __init__(self, pool: "asyncpg.Pool", settings: "Settings") -> None:
        self._pool     = pool
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._tick_errors: dict[str, int] = {}   # vehicle_id → consecutive error count

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name="spatial-worker")
        logger.info("SpatialWorker started (tick=%ss).",
                    self._settings.spatial_tick_interval_seconds)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Public API ────────────────────────────────────────────────────────────

    async def plot_course(self, vehicle_id: str, nav: NavComputerState) -> None:
        """
        Write a new NavComputerState to the database and set transit_state=in_transit.
        Call this from the action pipeline after Ollama resolves a navigation action.
        """
        now = datetime.now(timezone.utc)
        if nav.departure_at is None:
            nav = nav.model_copy(update={"departure_at": now})

        dist = _euclidean(nav.origin_x, nav.origin_y, nav.origin_z,
                          nav.dest_x,   nav.dest_y,   nav.dest_z)
        ticks_needed  = max(1, math.ceil(dist / nav.speed)) if nav.speed > 0 else 1
        eta_seconds   = ticks_needed * self._settings.spatial_tick_interval_seconds

        nav = nav.model_copy(update={
            "transit_state":      TransitState.IN_TRANSIT,
            "distance_total":     dist,
            "distance_remaining": dist,
            "current_x":          nav.origin_x,
            "current_y":          nav.origin_y,
            "current_z":          nav.origin_z,
            "eta_seconds":        eta_seconds,
        })

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vehicles
                SET transit_state = 'in_transit',
                    nav_computer  = $1::jsonb,
                    updated_at    = NOW()
                WHERE id = $2
                """,
                nav.model_dump_json(),
                UUID(vehicle_id),
            )
        logger.info("SpatialWorker: course plotted for vehicle %s → %s (%.0f units, ETA %ds).",
                    vehicle_id, nav.destination_name, dist, eta_seconds)

    # ── Background loop ───────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        interval = self._settings.spatial_tick_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self._tick_all_transits()
            except Exception as exc:
                logger.error("SpatialWorker tick error (non-fatal): %s", exc)

    async def _tick_all_transits(self) -> None:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text, campaign_id::text, nav_computer
                FROM   vehicles
                WHERE  transit_state = 'in_transit'
                """
            )

        for row in rows:
            vehicle_id  = row["id"]
            campaign_id = row["campaign_id"]
            if self._tick_errors.get(vehicle_id, 0) >= _MAX_TICK_ERRORS:
                continue
            try:
                await self._advance_vehicle(vehicle_id, campaign_id, row["nav_computer"])
                self._tick_errors.pop(vehicle_id, None)
            except Exception as exc:
                count = self._tick_errors.get(vehicle_id, 0) + 1
                self._tick_errors[vehicle_id] = count
                logger.warning("SpatialWorker: vehicle %s tick error #%d: %s",
                               vehicle_id, count, exc)

    async def _advance_vehicle(
        self, vehicle_id: str, campaign_id: str, nav_json: str
    ) -> None:
        raw = json.loads(nav_json) if isinstance(nav_json, str) else nav_json
        nav = NavComputerState.model_validate(raw)

        if nav.transit_state != TransitState.IN_TRANSIT:
            return

        # ── Fuel check ────────────────────────────────────────────────────────
        fuel_after = nav.fuel_remaining - nav.fuel_per_tick
        if fuel_after <= 0:
            await self._handle_fuel_empty(vehicle_id, campaign_id, nav)
            return

        # ── Move along route vector ───────────────────────────────────────────
        dist_remaining = nav.distance_remaining
        step           = min(nav.speed, dist_remaining)

        if dist_remaining <= 0 or step <= 0:
            await self._handle_arrival(vehicle_id, campaign_id, nav)
            return

        ratio   = step / nav.distance_total if nav.distance_total > 0 else 1.0
        dx      = nav.dest_x - nav.origin_x
        dy      = nav.dest_y - nav.origin_y
        dz      = nav.dest_z - nav.origin_z

        new_x = nav.current_x + dx * ratio
        new_y = nav.current_y + dy * ratio
        new_z = nav.current_z + dz * ratio
        new_dist_remaining = max(0.0, dist_remaining - step)

        ticks_left  = math.ceil(new_dist_remaining / nav.speed) if nav.speed > 0 else 0
        new_eta     = ticks_left * self._settings.spatial_tick_interval_seconds

        # ── Fuel warning ──────────────────────────────────────────────────────
        fuel_pct_before = nav.fuel_remaining / nav.fuel_capacity if nav.fuel_capacity > 0 else 1.0
        fuel_pct_after  = fuel_after          / nav.fuel_capacity if nav.fuel_capacity > 0 else 1.0
        if fuel_pct_after <= _FUEL_WARNING_THRESHOLD < fuel_pct_before:
            await self._log_transit_event(vehicle_id, campaign_id, TransitEvent(
                vehicle_id=vehicle_id, campaign_id=campaign_id,
                event_type=TransitEventType.FUEL_WARNING,
                x=new_x, y=new_y, z=new_z,
                sector_name=nav.destination_name,
                description=(
                    f"{nav.origin_name or 'vessel'} → {nav.destination_name}: "
                    f"fuel at {fuel_pct_after * 100:.0f}% — consider diverting."
                ),
                event_data={"fuel_remaining": fuel_after, "fuel_capacity": nav.fuel_capacity},
            ))

        # ── Hazard check ──────────────────────────────────────────────────────
        hazard = await self._check_hazards(campaign_id, new_x, new_y, new_z)
        if hazard:
            nav_updated = nav.model_copy(update={
                "current_x":          new_x,
                "current_y":          new_y,
                "current_z":          new_z,
                "distance_remaining": new_dist_remaining,
                "fuel_remaining":     fuel_after,
                "eta_seconds":        new_eta,
                "transit_state":      TransitState.INTERDICTED,
                "interdiction_hazard": hazard["name"],
            })
            await self._persist_nav(vehicle_id, nav_updated, new_state="interdicted")
            await self._handle_interdiction(vehicle_id, campaign_id, nav_updated, hazard)
            return

        # ── Normal advance ────────────────────────────────────────────────────
        nav_updated = nav.model_copy(update={
            "current_x":          new_x,
            "current_y":          new_y,
            "current_z":          new_z,
            "distance_remaining": new_dist_remaining,
            "fuel_remaining":     fuel_after,
            "eta_seconds":        new_eta,
        })

        await self._persist_nav(vehicle_id, nav_updated, new_state="in_transit")

        if new_dist_remaining <= 0:
            await self._handle_arrival(vehicle_id, campaign_id, nav_updated)

    # ── Event handlers ────────────────────────────────────────────────────────

    async def _handle_arrival(
        self, vehicle_id: str, campaign_id: str, nav: NavComputerState
    ) -> None:
        nav_done = nav.model_copy(update={
            "transit_state":      TransitState.DOCKED,
            "current_x":          nav.dest_x,
            "current_y":          nav.dest_y,
            "current_z":          nav.dest_z,
            "distance_remaining": 0.0,
            "eta_seconds":        0,
        })
        await self._persist_nav(vehicle_id, nav_done, new_state="docked")

        event = TransitEvent(
            vehicle_id=vehicle_id, campaign_id=campaign_id,
            event_type=TransitEventType.ARRIVAL,
            x=nav.dest_x, y=nav.dest_y, z=nav.dest_z,
            sector_name=nav.destination_name,
            description=f"Arrived at {nav.destination_name}.",
            event_data={"origin": nav.origin_name, "destination": nav.destination_name},
        )
        await self._log_transit_event(vehicle_id, campaign_id, event)

        embed = {
            "title": f"🚀 Arrived: {nav.destination_name}",
            "description": (
                f"The vessel has completed its journey from **{nav.origin_name}** "
                f"to **{nav.destination_name}**."
            ),
            "color": 0x00C851,
            "fields": [
                {"name": "Fuel Remaining",
                 "value": f"{nav.fuel_remaining:.1f} / {nav.fuel_capacity:.1f}",
                 "inline": True},
            ],
        }
        await self._send_discord_embed(campaign_id, embed)
        logger.info("SpatialWorker: vehicle %s arrived at %s.", vehicle_id, nav.destination_name)

    async def _handle_interdiction(
        self, vehicle_id: str, campaign_id: str, nav: NavComputerState,
        hazard: dict[str, Any]
    ) -> None:
        event = TransitEvent(
            vehicle_id=vehicle_id, campaign_id=campaign_id,
            event_type=TransitEventType.INTERDICTION,
            x=nav.current_x, y=nav.current_y, z=nav.current_z,
            sector_name=hazard.get("name", "Unknown Zone"),
            description=(
                f"⚠ Transit interrupted — entered {hazard.get('zone_type', 'hazard')} zone "
                f"'{hazard.get('name', 'Unknown')}'."
            ),
            event_data=hazard,
        )
        await self._log_transit_event(vehicle_id, campaign_id, event)

        embed = {
            "title": "⚠ TRANSIT INTERDICTED",
            "description": (
                f"Vessel intercepted in **{hazard.get('name', 'Unknown Zone')}** "
                f"({hazard.get('zone_type', 'hazard zone')}) while en route to "
                f"**{nav.destination_name}**.\n\n"
                "The GM has been alerted. Resume transit with `/resume_course`."
            ),
            "color": 0xFF4444,
            "fields": [
                {"name": "Coordinates",
                 "value": f"({nav.current_x:.1f}, {nav.current_y:.1f}, {nav.current_z:.1f})",
                 "inline": True},
                {"name": "Distance Remaining",
                 "value": f"{nav.distance_remaining:.1f} units",
                 "inline": True},
            ],
        }
        await self._send_discord_embed(campaign_id, embed)
        logger.info("SpatialWorker: vehicle %s interdicted at %s.", vehicle_id, hazard.get("name"))

    async def _handle_fuel_empty(
        self, vehicle_id: str, campaign_id: str, nav: NavComputerState
    ) -> None:
        nav_stalled = nav.model_copy(update={
            "transit_state":  TransitState.INTERDICTED,
            "fuel_remaining": 0.0,
            "interdiction_hazard": "fuel_exhaustion",
        })
        await self._persist_nav(vehicle_id, nav_stalled, new_state="interdicted")

        event = TransitEvent(
            vehicle_id=vehicle_id, campaign_id=campaign_id,
            event_type=TransitEventType.FUEL_EMPTY,
            x=nav.current_x, y=nav.current_y, z=nav.current_z,
            description=(
                f"Fuel exhausted {nav.distance_remaining:.1f} units from "
                f"{nav.destination_name}. Vessel adrift."
            ),
            event_data={"distance_remaining": nav.distance_remaining},
        )
        await self._log_transit_event(vehicle_id, campaign_id, event)

        embed = {
            "title": "⛽ FUEL EXHAUSTED",
            "description": (
                f"Vessel ran out of fuel **{nav.distance_remaining:.1f} units** "
                f"from **{nav.destination_name}**.\nThe ship is adrift and awaiting rescue."
            ),
            "color": 0xFF8C00,
        }
        await self._send_discord_embed(campaign_id, embed)
        logger.warning("SpatialWorker: vehicle %s fuel exhausted.", vehicle_id)

    # ── Database helpers ──────────────────────────────────────────────────────

    async def _persist_nav(
        self, vehicle_id: str, nav: NavComputerState, new_state: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vehicles
                SET nav_computer  = $1::jsonb,
                    transit_state = $2::transit_state,
                    updated_at    = NOW()
                WHERE id = $3
                """,
                nav.model_dump_json(),
                new_state,
                UUID(vehicle_id),
            )

    async def _check_hazards(
        self, campaign_id: str, x: float, y: float, z: float
    ) -> dict[str, Any] | None:
        """Return the first enabled hazard zone the coordinates fall inside, or None."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT name, zone_type, center_x, center_y, center_z, radius, zone_data
                FROM   hazard_zones
                WHERE  campaign_id = $1 AND enabled = TRUE
                """,
                UUID(campaign_id),
            )
        for row in rows:
            dist = _euclidean(x, y, z, row["center_x"], row["center_y"], row["center_z"])
            if dist <= row["radius"]:
                return {
                    "name":      row["name"],
                    "zone_type": row["zone_type"],
                    "radius":    row["radius"],
                    "zone_data": dict(row["zone_data"]) if row["zone_data"] else {},
                }
        return None

    async def _log_transit_event(
        self, vehicle_id: str, campaign_id: str, event: TransitEvent
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO transit_log
                    (vehicle_id, campaign_id, event_type, x, y, z,
                     sector_name, description, event_data)
                VALUES ($1, $2, $3::transit_event_type, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                UUID(vehicle_id),
                UUID(campaign_id),
                event.event_type.value,
                event.x, event.y, event.z,
                event.sector_name,
                event.description,
                json.dumps(event.event_data),
            )

    # ── Discord notification ──────────────────────────────────────────────────

    async def _send_discord_embed(
        self, campaign_id: str, embed: dict[str, Any]
    ) -> None:
        webhook_url = self._settings.spatial_discord_webhook_url
        if not webhook_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(webhook_url, json={"embeds": [embed]})
        except Exception as exc:
            logger.debug("SpatialWorker: Discord webhook failed (non-fatal): %s", exc)

    # ── Utility: fetch recent transit events for GM Director catch-up ─────────

    async def get_recent_events(
        self, campaign_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Returns the most recent transit events for a campaign.
        The GMDirector can inject these into the narrative context so players
        receive organic in-character updates about vessel movements.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tl.event_type, tl.description, tl.created_at,
                       v.name AS vehicle_name
                FROM   transit_log tl
                JOIN   vehicles v ON v.id = tl.vehicle_id
                WHERE  tl.campaign_id = $1
                ORDER  BY tl.created_at DESC
                LIMIT  $2
                """,
                UUID(campaign_id),
                limit,
            )
        return [dict(r) for r in rows]


# ── Pure-math helper ──────────────────────────────────────────────────────────

def _euclidean(
    x1: float, y1: float, z1: float,
    x2: float, y2: float, z2: float,
) -> float:
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)

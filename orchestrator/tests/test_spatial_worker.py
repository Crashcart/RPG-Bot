"""
Tests for SpatialWorker — Asynchronous Cargo Hauling & Spatial Routing (Issue #21)
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from orchestrator.schemas.payloads import (
    NavComputerState,
    TransitEventType,
    TransitState,
)
from orchestrator.services.spatial_worker import SpatialWorker, _euclidean


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def make_settings(tick_interval: int = 60, webhook: str = "") -> MagicMock:
    s = MagicMock()
    s.spatial_tick_interval_seconds = tick_interval
    s.spatial_discord_webhook_url   = webhook
    return s


def make_pool(fetch_result=None, execute_result=None):
    """Build a mock asyncpg pool whose .acquire() context manager works."""
    conn = AsyncMock()
    conn.fetch      = AsyncMock(return_value=fetch_result or [])
    conn.execute    = AsyncMock(return_value=execute_result)
    conn.fetchrow   = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__  = AsyncMock(return_value=False)
    return pool, conn


def make_nav(
    *,
    transit_state: TransitState = TransitState.IN_TRANSIT,
    origin_x: float = 0.0, origin_y: float = 0.0, origin_z: float = 0.0,
    dest_x: float = 100.0, dest_y: float = 0.0, dest_z: float = 0.0,
    current_x: float = 0.0, current_y: float = 0.0, current_z: float = 0.0,
    speed: float = 10.0,
    fuel_remaining: float = 100.0,
    fuel_capacity: float = 100.0,
    fuel_per_tick: float = 0.5,
    distance_remaining: float = 100.0,
    distance_total: float = 100.0,
    destination_name: str = "Colony Theta",
    origin_name: str = "Kepler Station",
) -> NavComputerState:
    return NavComputerState(
        transit_state=transit_state,
        origin_x=origin_x, origin_y=origin_y, origin_z=origin_z,
        dest_x=dest_x, dest_y=dest_y, dest_z=dest_z,
        current_x=current_x, current_y=current_y, current_z=current_z,
        speed=speed,
        fuel_remaining=fuel_remaining,
        fuel_capacity=fuel_capacity,
        fuel_per_tick=fuel_per_tick,
        distance_remaining=distance_remaining,
        distance_total=distance_total,
        destination_name=destination_name,
        origin_name=origin_name,
        eta_seconds=600,
        departure_at=datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Euclidean distance helper
# ─────────────────────────────────────────────────────────────────────────────

class TestEuclidean:
    def test_same_point(self):
        assert _euclidean(1, 2, 3, 1, 2, 3) == pytest.approx(0.0)

    def test_x_axis(self):
        assert _euclidean(0, 0, 0, 5, 0, 0) == pytest.approx(5.0)

    def test_3d(self):
        # sqrt(1^2 + 2^2 + 2^2) = sqrt(9) = 3
        assert _euclidean(0, 0, 0, 1, 2, 2) == pytest.approx(3.0)

    def test_negative_coords(self):
        assert _euclidean(-1, -1, -1, 1, 1, 1) == pytest.approx(math.sqrt(12))


# ─────────────────────────────────────────────────────────────────────────────
# plot_course
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotCourse:
    @pytest.mark.asyncio
    async def test_writes_in_transit_state(self):
        pool, conn = make_pool()
        worker = SpatialWorker(pool=pool, settings=make_settings())
        nav = make_nav(origin_x=0, dest_x=100, speed=10.0)

        await worker.plot_course(str(uuid4()), nav)

        conn.execute.assert_awaited_once()
        call_args = conn.execute.call_args[0]
        written_nav = json.loads(call_args[1])
        assert written_nav["transit_state"] == "in_transit"

    @pytest.mark.asyncio
    async def test_computes_distance_and_eta(self):
        pool, conn = make_pool()
        worker = SpatialWorker(pool=pool, settings=make_settings(tick_interval=60))
        nav = make_nav(origin_x=0, dest_x=100, speed=10.0)

        await worker.plot_course(str(uuid4()), nav)

        written = json.loads(conn.execute.call_args[0][1])
        assert written["distance_total"] == pytest.approx(100.0)
        # 100 / 10 = 10 ticks × 60s = 600s
        assert written["eta_seconds"] == 600

    @pytest.mark.asyncio
    async def test_sets_departure_at_when_missing(self):
        pool, conn = make_pool()
        worker = SpatialWorker(pool=pool, settings=make_settings())
        nav = make_nav()
        nav = nav.model_copy(update={"departure_at": None})

        await worker.plot_course(str(uuid4()), nav)

        written = json.loads(conn.execute.call_args[0][1])
        assert written["departure_at"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Normal transit advance
# ─────────────────────────────────────────────────────────────────────────────

class TestAdvanceVehicle:
    @pytest.mark.asyncio
    async def test_coordinates_advance_toward_destination(self):
        nav = make_nav(
            origin_x=0, dest_x=100, current_x=0,
            distance_remaining=100, distance_total=100, speed=10.0,
            fuel_remaining=50.0, fuel_per_tick=1.0,
        )
        pool, conn = make_pool(fetch_result=[])  # no hazards
        worker = SpatialWorker(pool=pool, settings=make_settings())

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        conn.execute.assert_awaited()
        written = json.loads(conn.execute.call_args[0][1])
        assert written["current_x"] > 0, "X should have advanced"
        assert written["distance_remaining"] == pytest.approx(90.0)
        assert written["transit_state"] == "in_transit"

    @pytest.mark.asyncio
    async def test_fuel_decremented_each_tick(self):
        nav = make_nav(fuel_remaining=50.0, fuel_per_tick=5.0,
                       distance_remaining=100, speed=10.0)
        pool, conn = make_pool(fetch_result=[])
        worker = SpatialWorker(pool=pool, settings=make_settings())

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        written = json.loads(conn.execute.call_args[0][1])
        assert written["fuel_remaining"] == pytest.approx(45.0)

    @pytest.mark.asyncio
    async def test_skips_if_not_in_transit(self):
        nav = make_nav(transit_state=TransitState.DOCKED)
        pool, conn = make_pool()
        worker = SpatialWorker(pool=pool, settings=make_settings())

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        conn.execute.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# Arrival
# ─────────────────────────────────────────────────────────────────────────────

class TestArrival:
    @pytest.mark.asyncio
    async def test_arrival_sets_docked_state(self):
        nav = make_nav(
            distance_remaining=5.0, speed=10.0,  # one step overshoots
            fuel_remaining=50.0, fuel_per_tick=1.0,
        )
        pool, conn = make_pool(fetch_result=[])
        worker = SpatialWorker(pool=pool, settings=make_settings(webhook=""))

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        # Two execute calls: persist_nav + transit_log INSERT
        calls = conn.execute.call_args_list
        assert len(calls) >= 2
        written = json.loads(calls[0][0][1])
        assert written["transit_state"] == "docked"
        assert written["distance_remaining"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_arrival_sends_discord_embed(self):
        nav = make_nav(distance_remaining=5.0, speed=10.0, fuel_remaining=50.0)
        pool, conn = make_pool(fetch_result=[])
        worker = SpatialWorker(pool=pool, settings=make_settings(webhook="http://hook"))

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__  = AsyncMock(return_value=False)

            await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        mock_client.post.assert_awaited_once()
        body = mock_client.post.call_args[1]["json"]
        assert body["embeds"][0]["title"].startswith("🚀 Arrived")


# ─────────────────────────────────────────────────────────────────────────────
# Hazard / Interdiction
# ─────────────────────────────────────────────────────────────────────────────

class TestInterdiction:
    def _hazard_row(self, cx=50.0, cy=0.0, cz=0.0, radius=20.0):
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "name": "Pirate Patrol Alpha",
            "zone_type": "pirate_patrol",
            "center_x": cx, "center_y": cy, "center_z": cz,
            "radius": radius,
            "zone_data": {},
        }[k]
        return row

    @pytest.mark.asyncio
    async def test_interdiction_triggered_when_inside_hazard(self):
        # Vehicle starts at x=0 and moves 10 units toward x=100
        # Hazard zone centred at x=5 with radius=20 — vehicle will enter it
        nav = make_nav(
            current_x=0, dest_x=100, distance_remaining=100, speed=10.0,
            fuel_remaining=50.0, fuel_per_tick=1.0,
        )
        hazard_row = self._hazard_row(cx=5.0, cy=0.0, cz=0.0, radius=20.0)

        pool, conn = make_pool(fetch_result=[hazard_row])
        worker = SpatialWorker(pool=pool, settings=make_settings())

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        written = json.loads(conn.execute.call_args_list[0][0][1])
        assert written["transit_state"] == "interdicted"
        assert written["interdiction_hazard"] == "Pirate Patrol Alpha"

    @pytest.mark.asyncio
    async def test_no_interdiction_outside_hazard(self):
        # Hazard zone far from route
        nav = make_nav(
            current_x=0, dest_x=100, distance_remaining=100, speed=10.0,
            fuel_remaining=50.0, fuel_per_tick=1.0,
        )
        hazard_row = self._hazard_row(cx=500.0, cy=500.0, cz=500.0, radius=10.0)

        pool, conn = make_pool(fetch_result=[hazard_row])
        worker = SpatialWorker(pool=pool, settings=make_settings())

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        written = json.loads(conn.execute.call_args_list[0][0][1])
        assert written["transit_state"] == "in_transit"


# ─────────────────────────────────────────────────────────────────────────────
# Fuel exhaustion
# ─────────────────────────────────────────────────────────────────────────────

class TestFuelExhaustion:
    @pytest.mark.asyncio
    async def test_fuel_empty_triggers_interdiction(self):
        nav = make_nav(fuel_remaining=0.4, fuel_per_tick=1.0,
                       distance_remaining=50, speed=10.0)
        pool, conn = make_pool(fetch_result=[])
        worker = SpatialWorker(pool=pool, settings=make_settings())

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        written = json.loads(conn.execute.call_args_list[0][0][1])
        assert written["transit_state"] == "interdicted"
        assert written["fuel_remaining"] == pytest.approx(0.0)
        assert written["interdiction_hazard"] == "fuel_exhaustion"

    @pytest.mark.asyncio
    async def test_fuel_warning_logged_on_threshold_crossing(self):
        # Fuel is at 22% → will drop to 17% (crosses 20% threshold)
        nav = make_nav(
            fuel_remaining=22.0, fuel_capacity=100.0, fuel_per_tick=5.0,
            distance_remaining=90, speed=10.0,
        )
        pool, conn = make_pool(fetch_result=[])
        worker = SpatialWorker(pool=pool, settings=make_settings())

        await worker._advance_vehicle(str(uuid4()), str(uuid4()), nav.model_dump_json())

        # First execute call should be the fuel warning INSERT, then the nav update
        insert_calls = [
            c for c in conn.execute.call_args_list
            if "INSERT INTO transit_log" in c[0][0]
        ]
        assert len(insert_calls) >= 1
        assert "fuel_warning" in insert_calls[0][0][3]  # event_type arg


# ─────────────────────────────────────────────────────────────────────────────
# Tick loop error handling
# ─────────────────────────────────────────────────────────────────────────────

class TestTickLoop:
    @pytest.mark.asyncio
    async def test_vehicle_skipped_after_max_errors(self):
        pool, conn = make_pool()
        worker = SpatialWorker(pool=pool, settings=make_settings())
        vid = str(uuid4())
        worker._tick_errors[vid] = 5  # already at limit

        nav = make_nav()
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": vid, "campaign_id": str(uuid4()),
            "nav_computer": nav.model_dump_json()
        }[k]

        pool2, conn2 = make_pool(fetch_result=[row])
        worker._pool = pool2

        await worker._tick_all_transits()
        conn2.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_error_counter_clears_on_success(self):
        nav = make_nav(distance_remaining=100, speed=10.0, fuel_remaining=50.0)
        vid = str(uuid4())
        cid = str(uuid4())

        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": vid, "campaign_id": cid,
            "nav_computer": nav.model_dump_json()
        }[k]

        pool, conn = make_pool(fetch_result=[row])
        # hazard check also uses fetch — return empty list
        conn.fetch.side_effect = [
            [row],   # _tick_all_transits outer fetch
            [],      # _check_hazards inner fetch
        ]

        worker = SpatialWorker(pool=pool, settings=make_settings())
        worker._tick_errors[vid] = 2  # some prior errors

        await worker._tick_all_transits()
        assert worker._tick_errors.get(vid, 0) == 0


# ─────────────────────────────────────────────────────────────────────────────
# get_recent_events
# ─────────────────────────────────────────────────────────────────────────────

class TestGetRecentEvents:
    @pytest.mark.asyncio
    async def test_returns_rows_as_dicts(self):
        row = MagicMock()
        row.keys = MagicMock(return_value=["event_type", "description", "vehicle_name",
                                            "created_at"])
        row.__getitem__ = lambda self, k: {
            "event_type": "arrival", "description": "Arrived at Colony.",
            "vehicle_name": "Dustrunner", "created_at": datetime.now(timezone.utc),
        }[k]

        pool, conn = make_pool(fetch_result=[row])
        # asyncpg rows support dict() via dict(row) — mock returns list[MagicMock]
        # Our impl calls dict(r) for each row; patch __iter__ on MagicMock
        pool2, conn2 = make_pool(fetch_result=[])
        conn2.fetch = AsyncMock(return_value=[
            {"event_type": "arrival", "description": "Arrived.",
             "vehicle_name": "Dustrunner", "created_at": datetime.now(timezone.utc)}
        ])
        pool2.acquire.return_value.__aenter__ = AsyncMock(return_value=conn2)

        worker = SpatialWorker(pool=pool2, settings=make_settings())
        events = await worker.get_recent_events(str(uuid4()), limit=5)

        assert len(events) == 1
        assert events[0]["event_type"] == "arrival"

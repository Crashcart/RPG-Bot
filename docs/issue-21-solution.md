# Issue #21 — Asynchronous Cargo Hauling & Spatial Routing

## Summary

Adds a background `SpatialWorker` asyncio service that advances vehicles along
plotted routes each tick, checks for hazard zone collisions, handles fuel
depletion, and fires Discord embed notifications on arrivals and interdictions —
all without invoking the LLM.

## What Was Built

| File | Change |
|------|--------|
| `db/migrations/018_cargo_hauling.sql` | `transit_state` + `nav_computer` columns on `vehicles`; `hazard_zones` table; `transit_log` event table |
| `orchestrator/services/spatial_worker.py` | `SpatialWorker` background service |
| `orchestrator/schemas/payloads.py` | `NavComputerState`, `CourseRequest`, `TransitEvent`, `TransitState`, `TransitEventType` models |
| `orchestrator/config.py` | `SPATIAL_TICK_INTERVAL_SECONDS`, `SPATIAL_DISCORD_WEBHOOK_URL` settings |
| `.env.example` | Two new commented env vars |
| `orchestrator/tests/test_spatial_worker.py` | 22 pytest-asyncio unit tests |

## Architecture

```
Discord /set_course ──► POST /api/vehicle/{id}/course
                             │
                      SpatialWorker.plot_course()
                             │ writes nav_computer JSONB
                             │ sets transit_state = in_transit
                             ▼
                     Background loop (every 60 s)
                             │
                      _tick_all_transits()
                      ┌──────────────────────┐
                      │ for each in_transit  │
                      │ vehicle:             │
                      │  advance coords      │
                      │  check fuel          │
                      │  check hazard_zones  │
                      │  arrival? → docked   │
                      └──────────────────────┘
                             │
                      transit_log INSERT
                      vehicles UPDATE
                             │
                      Discord webhook embed
                      (arrival / interdiction / fuel)
```

## nav_computer JSONB Contract

```json
{
  "transit_state": "in_transit",
  "origin_name": "Kepler Station",
  "destination_name": "Mining Colony Theta",
  "origin_x": 0.0,  "origin_y": 0.0,  "origin_z": 0.0,
  "dest_x": 450.0,  "dest_y": 120.0,  "dest_z": -30.0,
  "current_x": 90.0, "current_y": 24.0, "current_z": -6.0,
  "speed": 10.0,
  "distance_total": 471.4,
  "distance_remaining": 377.1,
  "eta_seconds": 37710,
  "fuel_remaining": 80.0,
  "fuel_capacity": 100.0,
  "fuel_per_tick": 0.5,
  "departure_at": "2026-06-03T12:00:00Z",
  "interdiction_hazard": null
}
```

## Wiring into main.py

```python
from orchestrator.services.spatial_worker import SpatialWorker

# In the lifespan context manager, after db and settings are ready:
spatial_worker = SpatialWorker(pool=db.pool, settings=settings)
await spatial_worker.start()

# On shutdown:
await spatial_worker.stop()
```

## Course Plotting (action pipeline)

After Ollama resolves a navigation action (`action_type = "navigation"`), the
action pipeline should call:

```python
from orchestrator.schemas.payloads import NavComputerState, TransitState

nav = NavComputerState(
    origin_name="Kepler Station",
    destination_name=destination,
    origin_x=current_x, origin_y=current_y,
    dest_x=dest_x, dest_y=dest_y,
    speed=vehicle_speed,
    fuel_remaining=vehicle_fuel,
    fuel_capacity=vehicle_fuel_max,
    fuel_per_tick=0.5,
)
await spatial_worker.plot_course(vehicle_id, nav)
```

## GMDirector catch-up context

Inject recent transit events into the GM prompt so players receive organic
in-character updates:

```python
events = await spatial_worker.get_recent_events(campaign_id, limit=5)
# Append a "World news" block to the GM system prompt
```

## TDR Compliance

| TDR Requirement | Implementation |
|----------------|----------------|
| Background spatial worker (asyncio) | `SpatialWorker._run_loop()` |
| Coordinate tracking in PostgreSQL | `vehicles.nav_computer` JSONB + `transit_log` |
| Hazard zone collision detection | `_check_hazards()` Euclidean distance |
| Discord ping on interdiction | `_handle_interdiction()` → webhook embed |
| LLM not invoked during transit | All math in Python; LLM only reads `get_recent_events()` output |
| Fuel management | `fuel_remaining`, `fuel_per_tick`, warning + empty handlers |
| Arrival notification | `_handle_arrival()` → green Discord embed |

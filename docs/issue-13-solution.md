# ABES — Autonomous Background Entity Simulation

**Issue:** #13  
**Branch:** `claude/feat/issue-13-abes`  
**Service:** `orchestrator/services/abes_service.py`  
**Migration:** `db/migrations/014_abes.sql`  
**Tests:** `orchestrator/tests/test_abes.py`

## Summary

Implements a background world-tick engine that simulates NPC and faction
activity while players are offline. The engine is pure Python + CSPRNG — the
LLM is **never** invoked during a tick.

## Architecture

```
main.py lifespan
  └── asyncio.create_task(abes.run())
        └── every 60 s → _tick_all_campaigns()
              └── for each active campaign (tick_interval elapsed):
                    tick_campaign(campaign_id)
                      └── for each active NPC intent:
                            _process_npc_intent()
                              ├── _roll(20) — CSPRNG dice
                              ├── UPDATE npc_long_term_intents
                              └── INSERT world_delta

GMDirector.narrate()
  └── abes.get_recent_events(campaign_id, since=player_last_seen)
        └── injects as in-character rumours into narrative prompt
```

## Database Tables

| Table | Purpose |
|-------|---------|
| `npc_long_term_intents` | One row per NPC/faction background task |
| `world_delta` | Immutable event log; one row per tick outcome |
| `abes_config` | Per-campaign tick interval, time-dilation, webhook URL |

## Dice Resolution (no LLM)

| Roll (1d20) | Outcome |
|-------------|---------|
| 1 | Critical complication — 1d6+2 HP damage, progress stalls |
| 2–4 | Minor complication — 1d4 HP damage |
| 5–20 | Progress — gain = `max(1, (roll + adjusted) × dilation // 5)` |

HP ≤ 0 → entity dies (`status = 'failed'`, `event_type = 'death'`).  
Progress = 100 → task complete (`status = 'completed'`, `event_type = 'task_complete'`).

## Wiring into main.py

```python
# After db.connect() in lifespan:
from orchestrator.services.abes_service import WorldTickService
abes = WorldTickService(pool=db.pool)
abes_task = asyncio.create_task(abes.run())

# Inject into GMDirector for catch-up narration:
gm_director = GMDirector(..., abes=abes)

# In lifespan shutdown:
abes_task.cancel()
try:
    await abes_task
except asyncio.CancelledError:
    pass
```

## GMDirector catch-up usage

```python
recent = await abes.get_recent_events(
    campaign_id=campaign_id,
    since=player_last_active,
    limit=10,
    min_severity="major",
)
# Inject `recent` summaries into the narrative prompt as rumours
```

## Running tests

```bash
pip install -r requirements-dev.txt
pytest orchestrator/tests/test_abes.py -v
```

## TDR Compliance

| Requirement | Implementation |
|-------------|----------------|
| No LLM during ticks | `_process_npc_intent` uses `secrets.randbelow` only |
| Discord push for major events | `_fire_webhook` (fails silently on network error) |
| World Delta table | `world_delta` — immutable event log |
| Time-dilation | `time_dilation` multiplier on progress gain |
| Per-campaign config | `abes_config` table with `enabled`, `tick_interval_s`, `webhook_url` |
| Offline event catch-up | `get_recent_events()` for GMDirector narrative injection |

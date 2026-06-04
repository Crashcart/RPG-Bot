# Issue #24 — Async Market Maker & Deep Supply-Chain Simulation

## Summary

Implements a background `EconomyWorker` service that drives a fully dynamic
economy across all active campaigns. On every tick (default 1 hour), production
and consumption rates update commodity supply at each `market_node`, prices are
recalculated, and Ghost Freighters equalize extreme market disparities to
prevent permanent market collapse.

## New Files

| File | Purpose |
|------|--------|
| `db/migrations/019_market_maker.sql` | 5 new tables + `market_node_type` enum |
| `orchestrator/services/economy_worker.py` | `EconomyWorker` background service |
| `orchestrator/tests/test_economy_worker.py` | 24 pytest-asyncio unit tests |

## Modified Files

| File | Change |
|------|--------|
| `orchestrator/services/__init__.py` | `EconomyWorker` export |
| `orchestrator/config.py` | `economy_tick_interval_seconds`, `economy_discord_webhook_url` |
| `.env.example` | Economy env var documentation |

## Architecture

```
Player action
  └─> Phase 2 Adjudication (Ollama)
        └─> EconomyWorker.get_live_price()  ← live price lookup
        └─> EconomyWorker.execute_transaction()  ← atomic supply update

[Background — every ECONOMY_TICK_INTERVAL_SECONDS]
  EconomyWorker._run_loop()
    └─> _tick_all_campaigns()
          └─> _tick_campaign(campaign_id)
                ├─> _tick_node(node_id)  ×N  [pure math, no LLM]
                ├─> _run_ghost_freighters(campaign_id)
                └─> _notify_price_spikes()  [Discord webhook, fail-silent]
```

## Database Schema

### `market_nodes`
Represents trade locations (cities, stations, outposts). Each node belongs to
one campaign and can be `is_enabled = FALSE` to temporarily pause trading.

### `commodities`
Per-campaign catalog of tradeable goods. `base_price` is the reference price
at 50% supply. `is_legal = FALSE` marks contraband that triggers a security
check during Phase 2 adjudication.

### `market_inventory`
The live state table. One row per (node, commodity) pair.
- `production_rate` — units added per tick
- `demand_rate` — units consumed per tick
- `current_price` — recalculated each tick

### `market_transactions`
Immutable audit log. Includes player buys/sells and ghost hauls.

### `economy_tick_log`
One row per tick execution. Useful for diagnosing tick frequency and ghost
freighter activity.

## Price Formula

```
price = base_price × (max_supply / 2) / max(supply, max_supply × 0.05)

At 50% supply → base_price       (reference point)
At 10% supply → 5 × base_price
At  5% supply → 10 × base_price  (floor of denominator)
At  0% supply → 10 × base_price  (floored denominator prevents ÷0)
At 100% supply → 0.5 × base_price

Capped at:  20 × base_price
Floored at: 0.2 × base_price
```

## Ghost Freighter Logic (TDR §4 Option 1)

After each tick, the worker queries for any commodity where
`price_at_dest / price_at_src > 3.0`. It then:
1. Calculates `haul_qty = min(src_max × 10%, src_supply, dst_remaining_capacity)`
2. Transfers units from source to destination
3. Recalculates prices at both nodes
4. Writes a `ghost_haul` transaction to `market_transactions`

This prevents the economy from collapsing when players ignore certain sectors.

## Wiring Into `main.py`

```python
# In lifespan startup:
from orchestrator.services import EconomyWorker
economy_worker = EconomyWorker(settings=settings, pool=db_pool)
await economy_worker.start()

# In lifespan shutdown:
await economy_worker.stop()
```

## Phase 2 Transaction Interception

When a player attempts to buy or sell cargo at a location, the adjudication
node should call `EconomyWorker` for the live price:

```python
# In adjudication.py, after identifying a trade action:
price = await economy_worker.get_live_price(node_id, commodity_id)
result = await economy_worker.execute_transaction(
    node_id=node_id,
    commodity_id=commodity_id,
    quantity=qty,
    action="buy",  # or "sell"
    character_id=character_id,
)
# Pass price + result into mechanical_truth for GM narration
```

## GM Narrative Context Injection

```python
# In gm_director.py, before building the system prompt:
market_entries = await economy_worker.get_market_context(campaign_id, limit=5)
if market_entries:
    economy_summary = "\n".join(
        f"- {e.node_name}: {e.commodity_name} at {e.current_price:.0f}cr "
        f"({e.price_ratio:.1f}× base)"
        for e in market_entries if e.price_ratio > 1.5  # only interesting entries
    )
    # Prepend to GM system prompt as world-state context
```

## TDR Compliance

| TDR Requirement | Implementation |
|---|---|
| Dynamic background economy | `EconomyWorker._run_loop()` asyncio background task |
| Hourly tick | `ECONOMY_TICK_INTERVAL_SECONDS` (default 3600) |
| Production / consumption rates | `production_rate` / `demand_rate` per inventory row |
| Exponential price scaling at low supply | Inverse-supply formula with cap/floor |
| Ghost Freighters | `_run_ghost_freighters()` — NPC haulers equalize disparities |
| LLM never invoked in tick | Pure Python math throughout `_tick_node()` |
| Transaction interception | `get_live_price()` + `execute_transaction()` |
| AI GM weaves economy into narrative | `get_market_context()` for GMDirector injection |
| PostgreSQL native math (materialized views) | `market_inventory` + standard asyncpg queries |
| FOSS tooling only | asyncpg + PostgreSQL — zero new infrastructure |

## Post-Merge Steps

1. Run `db/migrations/019_market_maker.sql` on staging/production PostgreSQL
2. Wire `EconomyWorker` into `orchestrator/main.py` lifespan (see above)
3. Optionally set `ECONOMY_DISCORD_WEBHOOK_URL` for Discord price-spike alerts
4. Seed initial `market_nodes` and `commodities` via the API or direct SQL
5. Call `economy_worker.get_live_price()` / `execute_transaction()` from Phase 2
   adjudication when trade actions are resolved

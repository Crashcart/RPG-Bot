# Issue #9 Solution: Multi-Tenant Campaign Vault

## Summary

Implements per-campaign SQLite isolation and hibernation as specified in the
Multi-Tenant Campaign Orchestration TDR (Issue #9).

## Context

- **Problem**: shared in-memory state can bleed between concurrent campaigns;
  a single PostgreSQL schema gives no isolation at the worker level.
- **Solution**: isolated SQLite files + hibernation snapshot for idle campaigns.
- **Repository**: `crashcart/rpg-bot`

## Architecture

```
/app/data/vault/
  scribe_core.db               ← RealityWall (world/genre state, existing)
  campaign_<uuid>.db           ← CampaignVault per-campaign files (NEW)
  campaign_<uuid>.db
  ...
```

Each `campaign_<uuid>.db` contains two tables:

| Table | Purpose |
|---|---|
| `kv_cache` | Lightweight per-campaign key-value store |
| `session_snapshot` | Single-row hibernation snapshot |

## New Files

| File | Purpose |
|---|---|
| `orchestrator/services/campaign_vault.py` | `CampaignVault` service |
| `orchestrator/tests/test_campaign_vault.py` | 20 pytest-asyncio unit tests |
| `db/migrations/014_campaign_vault.sql` | `campaign_vaults` PostgreSQL registry |
| `docs/issue-9-solution.md` | This document |

## Modified Files

| File | Change |
|---|---|
| `orchestrator/services/__init__.py` | Added `CampaignVault` to imports and `__all__` |

## API

```python
vault = CampaignVault(data_dir="/app/data")
await vault.init()

# Provision a campaign vault
await vault.provision(campaign_id)

# Per-campaign KV store
await vault.kv_set(campaign_id, "active_quest", {"id": "q1", "stage": 2})
quest = await vault.kv_get(campaign_id, "active_quest")
await vault.kv_delete(campaign_id, "active_quest")

# Hibernation
await vault.hibernate(campaign_id, {"session_token": tok, "turn": n})
snap = await vault.rehydrate(campaign_id)
await vault.clear_snapshot(campaign_id)

# Teardown
await vault.destroy(campaign_id)
campaign_ids = await vault.list_vaults()
```

## Wiring in main.py

```python
from orchestrator.services.campaign_vault import CampaignVault

# In lifespan startup:
vault = CampaignVault(data_dir=settings.data_dir)
await vault.init()
app.state.campaign_vault = vault
```

Register in the DB after campaign creation:

```python
await db.execute(
    "INSERT INTO campaign_vaults(campaign_id, vault_path) VALUES($1, $2)",
    campaign_id,
    f"vault/campaign_{campaign_id}.db",
)
```

## Hibernation Pattern

```python
# When a campaign goes idle (HIBERNATE_IDLE_MINUTES = 15):
snapshot = build_session_snapshot(campaign_id)  # dict from Redis/memory
await vault.hibernate(campaign_id, snapshot)
await db.execute(
    "UPDATE campaign_vaults SET status='hibernated', hibernated_at=NOW() WHERE campaign_id=$1",
    campaign_id,
)

# When a player returns (cold start):
snap = await vault.rehydrate(campaign_id)
if snap:
    restore_session(campaign_id, snap)
    await vault.clear_snapshot(campaign_id)
await db.execute(
    "UPDATE campaign_vaults SET status='active', last_accessed=NOW() WHERE campaign_id=$1",
    campaign_id,
)
```

## Security

| Threat | Mitigation |
|---|---|
| Path traversal via `campaign_id` | `validate_campaign_id()` enforces UUID4 regex before any filesystem operation |
| Escaping vault root | `_safe_db_path()` uses `Path.relative_to()` — raises `VaultError` on escape |
| Cross-campaign data leak | Each campaign has its own `.db` file with no shared SQLite state |
| Malicious non-string input | `validate_campaign_id()` checks `isinstance(campaign_id, str)` first |

## TDR Compliance

| TDR Requirement | Implementation |
|---|---|
| Per-campaign database file | `campaign_{uuid}.db` at `/app/data/vault/` |
| UUID-based campaign routing | `validate_campaign_id()` enforces UUID4 format |
| Path traversal protection | `_safe_db_path()` via `Path.relative_to()` |
| Hibernation / cold-start | `hibernate()` / `rehydrate()` / `clear_snapshot()` |
| KV cache isolation | Per-campaign SQLite `kv_cache` table |
| Zero cross-contamination | Separate `.db` file per campaign — no shared state |

## Testing

20 pytest-asyncio tests across 5 test classes. All use real on-disk SQLite
in `pytest`'s `tmp_path` fixture for fast, realistic execution.

```
TestValidateCampaignId   (6)  — UUID validation, traversal attempts, type errors
TestProvision            (5)  — create, idempotency, invalid ID, destroy
TestKVStore              (7)  — set/get/overwrite/complex value/delete/noop
TestHibernation          (6)  — hibernate/rehydrate/overwrite/clear/noop
TestIsolation            (4)  — campaign A vs B isolation, list, empty, snapshot isolation
```

Run:

```bash
pytest orchestrator/tests/test_campaign_vault.py -v
```

## Assumptions

- `campaign_id` values are UUID4 strings, consistent with `campaigns.id UUID PRIMARY KEY`.
- The vault root (`/app/data/vault/`) may already exist (managed by `RealityWall.init()`);
  `CampaignVault.init()` is idempotent.
- Migration 014 is the next sequential number on `main` (latest on main: 013).

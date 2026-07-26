# Test Coverage: PropheticBuffer & JanitorService

## Summary

Adds 67 unit tests across two new test modules covering `PropheticBuffer` and `JanitorService` — two of the five Critical Logic Modules listed in CLAUDE.md that had zero test coverage.

**Files added:**
- `orchestrator/tests/conftest.py` — importlib-based loader that bypasses the 31-service import chain
- `orchestrator/tests/test_prophetic_buffer.py` — 33 tests for `PropheticBuffer`
- `orchestrator/tests/test_janitor.py` — 34 tests for `JanitorService`
- `requirements-dev.txt` — pins `pytest==8.3.4` and `pytest-asyncio==0.24.0`

## Context

`orchestrator/services/__init__.py` eagerly imports all 31 services, pulling in `asyncpg`, `redis`, `chromadb`, `google.generativeai`, and similar heavy dependencies. Running `pytest` against any module in `orchestrator/services/` would fail immediately with `ModuleNotFoundError` in a dev environment that lacks the full production dependency set.

## Approach

`conftest.py` uses `importlib.util.spec_from_file_location()` to load each target module directly from its `.py` file path, completely skipping `__init__.py`. Loaded modules are patched into `sys.modules` under their real dotted names (`orchestrator.services.prophetic_buffer`, etc.) and a synthetic `orchestrator.services` package is wired up so `unittest.mock.patch("orchestrator.services.prophetic_buffer._X", ...)` can resolve the dotted path correctly.

This pattern lets tests run with only `pytest` and `pytest-asyncio` installed — no production dependencies required.

## Test Coverage

### PropheticBuffer (`test_prophetic_buffer.py`, 33 tests)

| Class | Tests |
|---|---|
| `TestFollowUpMap` | All `ActionOutcome` values covered, non-empty lists, string entries, critical_success/failure spot-checks |
| `TestAmbientPrediction` | Combat keys → `combat_tension`, social → `tavern_chatter`, area move → `dungeon_ambience`, recover/regroup → `campfire_quiet` |
| `TestEnqueue` | Happy path adds to queue, backpressure drop at `_MAX_QUEUE`, non-blocking return |
| `TestCacheReads` | `get_prefetched_text` and `get_prefetched_audio`: cache hit, miss, and exception (fail-open) |
| `TestCacheSet` | Correct TTL, exception swallowed |
| `TestPrefetch` | Audio key written for known outcome, text snippet on storyteller success, empty-string skip, timeout graceful, no audio for unknown follow-up, first follow-up used as primary, default fallback for missing outcome |
| `TestLifecycle` | `start()` creates non-done task, `stop()` cancels it, `is_busy` false when idle / true during prefetch, worker loop survives prefetch exception |

### JanitorService (`test_janitor.py`, 34 tests)

| Class | Tests |
|---|---|
| `TestRunBackup` | Backup created, WAL/SHM companions copied, idempotent on missing companions, missing source handled gracefully |
| `TestEnforceGFS` | Daily limit enforced, monthly limit enforced, total count bounded, recent backups preferred, Sunday-only weekly selection |
| `TestRunPrune` | `.png/.mp3/.mp4` pruned beyond 30 days, recent files kept, non-media extensions ignored, subdirectories walked recursively, missing prune dir is no-op, all three extensions independently checked |
| `TestRunLogRotation` | Logs older than 7 days gzip-compressed, recent logs untouched, `.gz` files older than 90 days deleted, recent `.gz` kept, `.log` files older than 90 days deleted, recent logs not deleted |
| `TestManualTriggers` | `trigger_backup()` calls `_run_backup()` and `_enforce_gfs()`, `trigger_prune()` calls `_run_prune()` |
| `TestLifecycle` | `start()` spawns 3 named tasks, `stop()` cancels all, `is_running` reflects state |

## Running the Tests

```bash
pip install -r requirements-dev.txt
python -m pytest orchestrator/tests/ -v
```

Expected output: `67 passed`

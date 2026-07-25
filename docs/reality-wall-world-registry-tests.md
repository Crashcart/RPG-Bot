# Test Suite: RealityWall & WorldRegistry

Two CLAUDE.md Critical Logic Modules now have full unit-test coverage.

## Files added

| File | Tests | What it validates |
|------|-------|-------------------|
| `orchestrator/tests/conftest.py` | — | Stubs env vars + missing optional deps so test imports work without a live stack |
| `orchestrator/tests/test_reality_wall.py` | 35 | Full coverage of `RealityWall` |
| `orchestrator/tests/test_world_registry.py` | 45 | Full coverage of `WorldRegistry` + `WorldSchema` |

**Total: 80 tests — all passing.**

## RealityWall coverage (35 tests)

| Class | Tests |
|-------|-------|
| `TestInit` | Directory tree creation, SQLite file creation, WAL mode active, idempotent init |
| `TestWorldRegistration` | Register creates DB entry + handouts/ + echo_vault/ dirs, list, idempotent, metadata, empty list |
| `TestCampaignWorldBinding` | Set/get roundtrip, unknown campaign → None, auto-register on set, rebinding, multi-campaign isolation |
| `TestDriftnetChannel` | Set/get roundtrip, unset → None, unknown world → None, update, auto-create world on set |
| `TestParadoxLevel` | Default=1, roundtrip, clamp <1, clamp >10, boundary 1, boundary 10, update, campaign isolation |
| `TestPathIsolation` | Correct handout/vault paths, traversal `../../` rejected, absolute escape rejected, nested subdir allowed |

## WorldRegistry coverage (45 tests)

| Class | Tests |
|-------|-------|
| `TestScan` | Empty dirs, fonts-only, templates-only, merged, dedup, cache population, sorted result |
| `TestMetadataPriority` | world.json loads, identity.json loads, override, empty value does not clobber, no-JSON minimal schema, malformed JSON graceful fallback (both tiers), system defaults to folder name, full world.json |
| `TestCacheMethods` | Empty before scan, sorted list after scan, get_schema hit/miss, reload re-reads disk |
| `TestManifest` | Creates dir + world.json, returns schema, existing → False, calls RealityWall, idempotent, minimal fields, adds to cache |
| `TestCampaignHelpers` | switch calls RealityWall, manifests new world, None when no world, returns schema |
| `TestSlugify` | 7 parametrized cases |
| `TestWorldSchemaProperties` | embed_color parses hex, default white, gm_tone_block both/tone-only/empty, driftnet default |

## Running

```bash
pip install pytest pydantic-settings
pytest orchestrator/tests/test_reality_wall.py orchestrator/tests/test_world_registry.py -v
# Expected: 80 passed
```

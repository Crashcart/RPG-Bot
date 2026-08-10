# Test Coverage: CampfireService, SystemIntegrityCheck & SandboxService

## Services covered

| Service | File | Purpose |
|---------|------|---------|
| `CampfireService` | `orchestrator/services/campfire.py` | Pauses story progression when key players go offline |
| `SystemIntegrityCheck` | `orchestrator/services/sic.py` | Four-pillar startup health verifier (paths, DB, GPU, permissions) |
| `SandboxService` | `orchestrator/services/sandbox.py` | Private GM testing interface for world architects |

None of these services appeared in CLAUDE.md and none were covered by previous test PRs (#68-#80).

## Test counts

| File | Tests |
|------|-------|
| `test_campfire_service.py` | 16 |
| `test_sic_service.py` | 22 |
| `test_sandbox_service.py` | 24 |
| **Total** | **62** |

## Running the tests

```bash
pip install pytest pytest-asyncio httpx
pytest orchestrator/tests/test_campfire_service.py \
       orchestrator/tests/test_sic_service.py \
       orchestrator/tests/test_sandbox_service.py -v
```

## What each suite covers

### CampfireService
- `update_presence` — online/offline player → campfire on/off
- No active campaign → inactive
- No ALIVE characters → inactive
- Missing presence row treated as offline
- Upsert SQL shape verified
- `get_status` → reads from system_settings with defaults
- `is_campfire_active` → truthy/falsy/missing row
- `force_campfire_on` / `force_campfire_off` → correct values written

### SystemIntegrityCheck
- `run()` → healthy / unstable / critical aggregation
- Pillar exception → forced critical fail
- `_persist` fire-and-forget verified; Redis errors swallowed
- Pillar 1 (paths): all present / DB missing / dirs missing (non-critical)
- Pillar 2 (database): valid DB / missing DB / corruption / executor exception
- Pillar 3 (GPU): VRAM detected / CPU-only / brain unreachable / unexpected error
- Pillar 4 (permissions): probe succeeds / PermissionError → critical fail
- Thread-safe helpers: `_sqlite_integrity_check`, `_permission_probe`

### SandboxService
- `chat()` basic — response dict shape, storyteller called, sandbox prompt used
- Persona → NPC persona prompt; echoed in result
- `use_search=True` → search called, injected in prompt; empty results → no block
- `image_url` → `generate_with_image` called; failure → graceful fallback
- Lore context → injected; failure → silent; capped at 6 facts
- Generation failure → error string returned (no raise)
- `_select_storyteller` → cloud / local / fallback-to-gemini

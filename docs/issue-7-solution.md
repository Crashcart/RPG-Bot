# Issue #7 — Persistent Visual & Textual Object State Tracker

## Summary

Adds a persistent memory system for in-game objects so the AI Game Master
never "forgets" what the cursed sword looked like three sessions ago or what
was inside the ornate chest the party found in Session 2.

## Architecture

### Storage layer

| Layer | Technology | Purpose |
|-------|-----------|--------|
| Primary store | PostgreSQL `world_objects` table (migration 014) | UUID-keyed object records with immutable description + mutable state |
| Index | GIN on `current_state` JSONB | Fast key/value queries |
| Cache | Redis (existing) | Optional future caching of hot objects |

### Schema

```sql
world_objects (
    entity_id        UUID  PK
    campaign_id      UUID  FK → campaigns
    base_description TEXT  -- immutable; set once at registration
    image_url        TEXT  -- path/URL to the canonical image asset
    current_state    JSONB -- mutable; arbitrary k/v state
    parent_entity_id UUID  -- self-FK for container nesting
    object_status    ENUM  -- active | locked | consumed | destroyed
    created_at, updated_at TIMESTAMPTZ
)
```

### Status lifecycle

```
active ──► locked      (puzzle box sealed, vault door closed)
       ──► consumed    (potion drunk, scroll cast) — TERMINAL
       ──► destroyed   (shield shattered, building burned) — TERMINAL
locked ──► active      (puzzle solved, vault unlocked)
```

Terminal statuses (`consumed`, `destroyed`) permanently block all mutations.
`locked` blocks mutations until explicitly unlocked via `set_status(ACTIVE)`.

### ObjectTracker service

**File:** `orchestrator/services/object_tracker.py`

| Method | Description |
|--------|------------|
| `register_object(campaign_id, base_description, image_url, parent_entity_id, initial_state)` | Create new object; returns `entity_id` string |
| `mutate_state(entity_id, state_patch, new_image_url)` | Shallow-merge patch into `current_state`; enforces status constraints |
| `set_status(entity_id, status)` | Lifecycle transition |
| `get_object(entity_id)` | Fetch single `WorldObjectRecord` |
| `get_children(parent_entity_id)` | List direct children (container contents) |
| `get_objects_for_campaign(campaign_id, status_filter, limit)` | Campaign-scoped listing |
| `get_context_summary(entity_id)` | Token-efficient one-line LLM string |
| `bulk_context_for_scene(entity_ids)` | Multi-line block for GM prompt injection |

### Context summary format

The `get_context_summary()` / `bulk_context_for_scene()` methods produce
tokens-efficient strings for direct injection into GM prompts:

```
A worn leather backpack, patched with copper rivets. (img:backpack_001.png) [active] — contents_count=3
A cloudy healing potion in a cracked vial. (img:potion_healing.png) [active] — charges=1
An iron shield with a split down the middle. (img:shield_broken.png) [destroyed]
```

### Pydantic schemas

**File:** `orchestrator/schemas/object_tracker_schemas.py`

- `WorldObjectStatus` — enum matching DB type
- `WorldObjectRecord` — full DB row representation
- `RegisterObjectRequest` — POST /api/objects body
- `MutateObjectRequest` — PATCH /api/objects/{id} body
- `ObjectContextSummary` — lightweight summary returned to callers

## Running the migration

```bash
psql $DATABASE_URL -f db/migrations/014_object_tracker.sql
```

## Wiring into the application

In `orchestrator/main.py` lifespan:

```python
from orchestrator.services.object_tracker import ObjectTracker

# During startup:
object_tracker = ObjectTracker(settings, pool)
app.state.object_tracker = object_tracker
```

In GMDirector (Phase 4), inject scene object context before narrative generation:

```python
if scene_entity_ids:
    object_context = await object_tracker.bulk_context_for_scene(scene_entity_ids)
    # Prepend object_context to the Gemini prompt's system block
```

Add API routes to `orchestrator/main.py`:

```python
@app.post("/api/objects")
async def register_object(req: RegisterObjectRequest, request: Request):
    tracker: ObjectTracker = request.app.state.object_tracker
    entity_id = await tracker.register_object(**req.model_dump())
    return {"entity_id": entity_id}

@app.get("/api/objects/{entity_id}")
async def get_object(entity_id: str, request: Request):
    tracker: ObjectTracker = request.app.state.object_tracker
    obj = await tracker.get_object(entity_id)
    if obj is None:
        raise HTTPException(status_code=404)
    return obj

@app.patch("/api/objects/{entity_id}")
async def mutate_object(entity_id: str, req: MutateObjectRequest, request: Request):
    tracker: ObjectTracker = request.app.state.object_tracker
    return await tracker.mutate_state(
        entity_id, req.state_patch, req.new_image_url
    )
```

## Tests

```bash
pytest orchestrator/tests/test_object_tracker.py -v
```

16 unit tests covering: registration, mutation (active / locked / consumed / destroyed),
status transitions, retrieval (single + children + empty), context summary
(description, image, state KVs, missing object), bulk context, and
`_format_summary` unit cases.

## TDR compliance

| TDR requirement | Implementation |
|-----------------|----------------|
| UUID identity for each object | `entity_id UUID PRIMARY KEY DEFAULT gen_random_uuid()` |
| Immutable `base_description` | Written at `INSERT`, never touched by `mutate_state` |
| `current_state` mutation | Shallow-merge via JSONB in `mutate_state()` |
| Container nesting | `parent_entity_id` self-FK; `get_children()` traverses one level |
| Destroyed/locked rejection | `ObjectMutationError` on terminal / locked status |
| Token-efficient LLM context | `get_context_summary()` / `bulk_context_for_scene()` |
| Image deduplication (Option 2) | `image_url` is a single canonical reference; consumers implement pHash before calling `register_object` |

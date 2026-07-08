# Issue #8 — Multi-Agent Vector-Space Communication (NATS Bus)

## Summary

Replaces sequential English-prompt NPC synchronisation with a NATS JetStream
message bus.  The GM Director publishes compressed `SceneStateVector` payloads
(integer emotion hashes + positional deltas) after every pipeline turn.  NPC
sub-agents consume these vectors directly without re-parsing full text prompts,
eliminating tokenization overhead and dramatically reducing inter-agent latency.

## Architecture

```
GM Director
    │
    ├── publish_scene_state()  →  aetheris.scene.{campaign_id}[.npc.{id}]
    ├── publish_combat_board() →  aetheris.combat.{campaign_id}
    └── publish_fog_update()   →  aetheris.fog.{campaign_id}

NPC Sub-Agents (Tier 2)
    │
    └── publish_npc_reaction() →  aetheris.npc.{npc_id}.react

GM Director (subscribe)
    └── subscribe_npc_reactions() ←  aetheris.npc.*.react
```

## New Files

| File | Purpose |
|------|---------|
| `orchestrator/schemas/nats_schemas.py` | Pydantic models: `EmotionHash`, `SceneStateVector`, `NpcReactionEvent`, `CombatBoardEvent`, `FogUpdateEvent` |
| `orchestrator/services/nats_bus.py` | `NatsBus` service with graceful degradation |
| `orchestrator/tests/test_nats_bus.py` | 18 pytest-asyncio unit tests (all mocked) |
| `db/migrations/014_nats_bus.sql` | `npc_entity_state` table |

## Emotion / Intent Hash Table

| Value | Name | Description |
|-------|------|-------------|
| 0 | NEUTRAL | Default, no strong emotion |
| 1 | AGGRO | Hostile / attacking |
| 2 | FEAR | Frightened, fleeing |
| 3 | CURIOUS | Investigating |
| 4 | SUSPICIOUS | On alert, not yet hostile |
| 5 | FRIENDLY | Allied or neutral-positive |
| 6 | PANIC | Uncontrolled fear |
| 7 | GUARD | Ordered defensive posture |
| 8 | INJURED | Wounded, impaired |
| 9 | DEAD | No further processing |
| 10 | CHARMED | Under magical compulsion |
| 11 | STUNNED | Temporarily incapacitated |

## TDR Compliance

| Requirement | Implementation |
|-------------|----------------|
| High-speed message bus | NATS JetStream (`ironclad-nats` Docker service) |
| Compressed state transmission | `SceneStateVector` with integer emotion hashes |
| Epistemic boundaries | `visible_to` filter on `publish_scene_state()` |
| Emotion/Intent Hashing | `EmotionHash` IntEnum (4-byte integers) |
| Security — prompt injection prevention | Pydantic serialisation only; no string eval |
| Graceful degradation | `NatsBus.is_ready=False` → all methods are no-ops |
| Parallel combat resolution | `publish_combat_board()` broadcasts to all combatants simultaneously |

## Wiring Guide (post-merge)

### 1. Add to `orchestrator/main.py` lifespan

```python
from orchestrator.services.nats_bus import NatsBus
from orchestrator.config import get_settings

settings = get_settings()
nats_bus = NatsBus(nats_url=settings.nats_url)

@asynccontextmanager
async def lifespan(app):
    await nats_bus.connect()
    # ... existing startup ...
    yield
    # ... existing shutdown ...
    await nats_bus.close()
```

### 2. Publish after each pipeline turn (in `narrate()`)

```python
if nats_bus.is_ready:
    vector = NatsBus.make_scene_vector(
        campaign_id=campaign_id,
        event_type=resolution.action_type,
        entity_emotions={},        # populate from npc_entity_state queries
        player_action=player_intent[:80],
    )
    asyncio.create_task(nats_bus.publish_scene_state(vector))
```

### 3. Run migration

```bash
psql $DATABASE_URL -f db/migrations/014_nats_bus.sql
```

### 4. Set environment variable

```bash
NATS_URL=nats://ironclad-nats:4222
```

Leave `NATS_URL` empty to run without NATS (graceful degradation).

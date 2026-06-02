# Issue #10 — FOSS Docker Ecosystem Stack: NATS Multi-Agent Message Bus

## Summary

This PR implements the NATS multi-agent message bus as specified in the issue #10 TDR. The existing stack already satisfies most of the TDR requirements (Ollama, PostgreSQL, Redis, ChromaDB). The only missing component was **NATS** as the lightweight inter-agent pub/sub bus.

## Context

The Aetheris stack before this PR:

| Component | TDR Requirement | Status |
|-----------|----------------|--------|
| Ollama (`brain`) | Local AI & Vision Engine | ✅ Already present |
| PostgreSQL 16 + `pgvector` extension | Multi-tenant DB + Vector RAG | ✅ Already present (pgvector via migration) |
| Redis (`ironclad-cache`) | Spatial state & volatile caching | ✅ Already present |
| ChromaDB (`ironclad-chroma`) | Rulebook RAG vector store | ✅ Already present |
| NATS (`ironclad-nats`) | Multi-agent message bus | ❌ **Missing — added by this PR** |

## Approach

Added NATS with JetStream enabled (`--jetstream`) so messages are durable. JetStream allows the scene-state stream to be replayed if a subscriber (NPC agent) restarts mid-session.

### Subject Hierarchy

```
aetheris.scene.{campaign_id}      — published after every pipeline turn (SceneStateEvent)
aetheris.npc.{npc_id}.react       — NPC reaction requests (NpcReactionEvent)
aetheris.combat.{campaign_id}     — combat board state broadcast (CombatBoardEvent)
aetheris.fog.{campaign_id}        — fog-of-war tile updates for map renderer
```

### Epistemic Boundary Enforcement

The `NatsBus.publish_scene_state()` helper accepts a `visible_to` set. When specified, only subjects for NPCs in that set receive the event. The GM always gets everything; NPCs only get vectors within their sensory radius (TDR §3 Selective Broadcasting).

## Files Changed

| File | Change |
|------|--------|
| `docker-compose.yml` | Added `ironclad-nats` service (NATS Alpine + JetStream); added `nats-data` volume |
| `orchestrator/config.py` | Added `nats_url`, `nats_jetstream_enabled` settings |
| `orchestrator/requirements.txt` | Added `nats-py==2.9.0` |
| `orchestrator/services/nats_bus.py` | New `NatsBus` service — connect/disconnect/publish/subscribe |
| `orchestrator/schemas/nats_schemas.py` | New Pydantic event schemas: `SceneStateEvent`, `NpcReactionEvent`, `CombatBoardEvent`, `FogUpdateEvent` |
| `.env.example` | Documented `NATS_URL` (optional, defaults to internal container URL) |
| `orchestrator/tests/test_nats_bus.py` | 18 unit tests — publish/subscribe/serialization/disconnect |

## Testing

All tests mock the NATS connection so no live NATS server is required:

```bash
cd orchestrator
pip install -r requirements.txt
pytest tests/test_nats_bus.py -v
```

## Wiring into main.py

To activate NatsBus in the FastAPI lifespan:

```python
# In orchestrator/main.py lifespan:
from orchestrator.services.nats_bus import NatsBus

nats_bus = NatsBus(settings)
await nats_bus.connect()      # startup
...
await nats_bus.disconnect()   # shutdown
```

## Assumptions

- NATS is optional at startup — if the server is unreachable, `NatsBus.connect()` logs a warning and sets `connected=False`. The pipeline continues without pub/sub (NPC agents simply won't receive scene events).
- JetStream stream `AETHERIS` is created automatically on first connect if it doesn't exist.
- The `PROJECT_PREFIX` env var (from issue #6 deploy.sh) is used for the container name: `${PROJECT_PREFIX:-aetheris}-nats`.

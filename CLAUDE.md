# Ironclad GM — Claude Code Guide

Self-hosted AI Game Master for tabletop RPGs on Discord. Four-phase pipeline: Ingestion → Adjudication (Ollama) → State Commit (PostgreSQL) → Narration (GM Director + Gemini).

## Project Layout

```
orchestrator/       FastAPI pipeline engine
  config.py         Pydantic settings (all env vars)
  schemas/
    payloads.py     Single source of truth for all inter-service payload schemas
  pipeline/
    ingestion.py    Phase 1 — character/inventory/rulebook context assembly
    adjudication.py Phase 2 — Ollama mechanical resolution
    state_commit.py Phase 3 — atomic PostgreSQL writes
    narration.py    Phase 4 — GM Director + sub-agent dispatch
  services/
    reality_wall.py     SQLite world-state store + path isolation
    paradox_engine.py   Unreliable narrator injection (paradox_level 1–10)
    prophetic_buffer.py Predictive asset pre-generation background worker
    janitor.py          GFS backup rotation + media auto-prune cron
    claude_client.py    Anthropic Claude Tier 1 storyteller (alt. to Gemini)
  prompts/          GM system prompts, immersion rules, guardrails
discord-bot/
  bot.py            Discord listener, embed delivery, immersion handlers
  voice_manager.py  Voice channel audio, ambient loops, TTS playback
db/
  schema.sql        Core tables
  migrations/       001–009 SQL migrations (latest: 009_admin_auth.sql)
csv-sync/           CSV export worker
media-proxy/        Static asset server (:8001)
health-sentinel/    Flask sidecar on :58291 — reports busy/ok status
lavalink/           Audio engine config
docker-compose.yml  Full stack (11 services)
```

## Critical Logic Modules

| Module | Role |
|--------|------|
| `RealityWall` | SQLite (WAL) world-state registry. Tracks `current_world` per campaign, enforces `data/handouts/{world}/` and `data/echo_vault/{world}/` path isolation. Also owns `paradox_level` per campaign. |
| `PropheticBuffer` | Fire-and-forget background worker enqueued after every pipeline turn. Pre-generates text snippets and ambient audio keys for the most likely follow-up action; results cached in Redis (TTL 120 s). |
| `ParadoxEngine` | Stateless post-processor applied by GMDirector after Step 4d. Scales unreliable-narrator artefacts to `paradox_level` 1–10 (1 = passthrough, 10 = full breakdown). |
| `JanitorService` | Two background loops: GFS backup (daily/weekly/monthly rotation of `reality_wall.db`) and media auto-prune (delete `.png/.mp3/.mp4` > 30 days from handouts + echo_vault). |
| `HealthSentinel` | Flask sidecar on `:58291`. Reads `ironclad:sentinel:busy` from Redis; returns `{"status":"busy"}` while Phase 2 AI adjudication is running, `{"status":"ok"}` otherwise. |

## Key Architectural Rules

- **Schemas are the contract.** All inter-service payloads live in `orchestrator/schemas/payloads.py`. Never pass raw dicts between pipeline phases — always use the Pydantic models defined there.
- **Ollama never narrates.** Phase 2 (adjudication) produces only mechanical output (`OllamaResolutionPayload`). Narrative prose is strictly Phase 4 (Gemini / GM Director).
- **Backend owns dice rolls.** The LLM requests a roll via `DiceRequest`; the backend generates the RNG result and injects it. The model cannot override dice outcomes.
- **Immutability of action_log.** Retcons set `retconned=TRUE` — rows are never deleted. The `retcon_log` table tracks all rollbacks.
- **Brand filtering.** Sub-agent output is post-processed to strip 150+ blocked brand terms. If stripping fails after retry, `brand_violation=True` is set on `SubAgentResult`.
- **Fair Play Sandbox.** Admin Discord accounts receive no mechanical privileges — they use the White Portal backchannel (`GMDirectiveRequest`) exclusively.
- **Node routing.** Use `NodeRouter` for all Ollama calls. Never hardcode a node URL — nodes are discovered from the `node_registry` table and auto-promoted by TTFT benchmarking.

## Services (orchestrator/main.py)

| Service | Purpose |
|---------|---------|
| `DatabaseService` | asyncpg connection pool |
| `CacheService` | Redis sessions / locks / pub-sub |
| `OllamaClient` | Multi-node Ollama HTTP client |
| `GeminiClient` | Google Gemini 1.5 Pro |
| `NodeRouter` | Node mesh, TTFT benchmarking, health checks |
| `RAGService` | ChromaDB rulebook retrieval |
| `GMDirector` | Tier 1 storyteller (plan → dispatch → synthesise) |
| `SubAgentDispatcher` | Tier 2 concurrent actor/scribe execution |
| `StoryMemoryService` | ChromaDB world-fact persistence |
| `TelemetryService` | WebSocket event stream for White Portal |
| `AuthService` | First-boot admin creation + session auth |

## Database (PostgreSQL 16)

Core tables: `campaigns`, `characters`, `inventories`, `action_log`, `sessions`, `story_facts`, `story_entities`, `vehicles`, `vehicle_subsystems`, `node_registry`, `global_settings`, `gm_directives`, `downtime_tasks`, `player_presence`, `retcon_log`, `admin_accounts`.

Always run new migrations as `db/migrations/0NN_<name>.sql`. Never modify existing migration files.

## Running Locally

```bash
cp .env.example .env        # fill in required vars
docker compose up --build   # starts all 10 services
```

Required env vars: `DISCORD_BOT_TOKEN`, `DISCORD_APPLICATION_ID`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `GEMINI_API_KEY`, `LAVALINK_PASSWORD`, `SESSION_SECRET_KEY`.

## API

- `POST /action` — main player action pipeline
- `POST /session` — create/refresh session
- `POST /api/directives` — GM backchannel directive
- `POST /api/downtime/submit` — async downtime task
- `GET /api/recap` — chronicle recap
- `POST /api/retcon` — admin rollback
- `POST /api/rulebook/ingest` — PDF upload
- `POST /api/vision/analyse` — image analysis (Gemini Vision)
- `POST /api/web/search` — web search (SerpAPI / DuckDuckGo)
- `WS /ws/telemetry` — live pipeline event stream
- `GET /web/*` — White Portal admin panel

## Development Branch

Active work lives on `claude/api-payload-schemas-0jkYr`. All Claude Code sessions should develop on branches prefixed `claude/` and push only to the designated branch.

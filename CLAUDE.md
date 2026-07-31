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
  migrations/       001–011 SQL migrations (latest: 011_settings_channels.sql)
csv-sync/           CSV export worker
media-proxy/        Static asset server (:8001)
health-sentinel/    Flask sidecar on :58291 — reports busy/ok status
lavalink/           Audio engine config
docker-compose.yml  Full stack (11 services)
```

## Critical Logic Modules

| Module | Role |
|--------|------|
| `RealityWall` | SQLite (WAL) world-state registry at `/app/data/vault/scribe_core.db`. Tracks `current_world` per campaign, `driftnet_channel_id` per world, `paradox_level` per campaign, and `data/handouts/{world}/` + `data/echo_vault/{world}/` path isolation. |
| `WorldRegistry` | Dynamic genre orchestration. Scans `data/fonts/` and `data/templates/` for world directories. Loads metadata with priority: `templates/{world}/identity.json` overrides `fonts/{world}/world.json` (TDR §3). Injects tone into GM prompts. `/switch_world` manifests new worlds on the fly. |
| `PropheticBuffer` | Fire-and-forget background worker enqueued after every pipeline turn. Pre-generates text snippets and ambient audio keys for the most likely follow-up action; results cached in Redis (TTL 120 s). |
| `ParadoxEngine` | Stateless post-processor applied by GMDirector after Step 4d. Scales unreliable-narrator artefacts to `paradox_level` 1–10 (1 = passthrough, 10 = full breakdown). |
| `JanitorService` | Python: two background asyncio loops (GFS backup + media prune). Alpine container `janitor/`: shell script equivalent per TDR §4. Both use GFS rotation (7 daily, 2 weekly, 1 monthly) and 30-day media prune for `.png/.mp3/.mp4`. |
| `HealthSentinel` | Flask sidecar on `:58291` (`health-sentinel/` service, named `pulse` in docker-compose). Reads `ironclad:sentinel:busy` from Redis; returns `{"status":"busy"}` while Phase 2 adjudication runs, `{"status":"ok"}` otherwise. |

## Dynamic Genre Orchestration (TDR §3 — Step 13 + 15)

**Zero-code system switching.** Worlds are discovered from two asset tiers at startup.

```
data/
  fonts/
    mothership/
      world.json      ← fallback metadata (Step 13)
    shadowrun/
      world.json
  templates/
    mothership/
      identity.json   ← TDR §3 primary metadata (overrides world.json)
    shadowrun/
      identity.json
    pirate_borg/      ← created automatically by /switch_world pirate_borg
      world.json      ←   auto-created under fonts/ when manifested
  handouts/
    mothership/       ← media isolation silo per world
  echo_vault/
    mothership/       ← audio isolation silo per world
  vault/
    scribe_core.db    ← RealityWall SQLite (WAL) database
```

**Metadata priority:** `templates/{world}/identity.json` fields override `fonts/{world}/world.json` when both exist. This allows TDR-aligned identity files to specify RGB/tone overrides without duplicating the full world definition.

**`identity.json` / `world.json` contract** (all fields optional except `display_name`):
```json
{
  "display_name":       "Pirate Borg",
  "primary_color":      "#FFD700",
  "narrative_tone":     "grimdark pirate chaos",
  "description":        "One paragraph of world context injected into every GM prompt.",
  "system":             "pirate_borg",
  "dice_notation":      "d6",
  "tags":               ["pirate", "grimdark", "horror"],
  "driftnet_channel_id": "123456789012345678"
}
```

**Driftnet channels:** Each world can have a dedicated Discord channel bound via `driftnet_channel_id`. When set, every GM narrative for that campaign is **mirrored** to the driftnet channel automatically (Step 7 of `_deliver_narrative`). Bind via `RealityWall.set_driftnet_channel()` or the `/switch_world` command.

**Discord commands:**
- `/worlds` — list all discovered worlds with tone + tags
- `/switch_world <name>` — raise the Reality Wall; creates the folder if it doesn't exist

**API endpoints:**
- `GET /api/worlds` — list all WorldSchema objects
- `POST /api/world/switch` — bind campaign to world (manifests if needed)
- `GET /api/world/{campaign_id}` — get active world schema for a campaign

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

Always run new migrations as `db/migrations/0NN_<name>.sql`. Never modify existing migration files. Latest migration: `011_settings_channels.sql`.

## TDR Compliance Notes (Step 15)

| Requirement | Implementation |
|-------------|----------------|
| Network: `aetheris_net` / `aetheris_store` | `docker-compose.yml` networks |
| Scribe (orchestrator) on `aetheris_net` + `aetheris_store` | `scribe` service |
| Brain (Ollama) Intel GPU `/dev/dri` passthrough | `brain` service `devices:` + `group_add: [video]` |
| Pulse (health-sentinel) on port 58291 | `pulse` service |
| Janitor Alpine container | `janitor` service (`janitor/Dockerfile` + `janitor/janitor.sh`) |
| SQLite WAL DB at `/app/data/vault/scribe_core.db` | `RealityWall.__init__` |
| Backups at `/app/backups` (separate from data volume) | `JanitorService.__init__` + docker-compose `./backups:/app/backups` |
| Logs at `/app/logs` | docker-compose `./logs:/app/logs` |
| `identity.json` in `templates/{genre}/` | `WorldRegistry._load_from_disk()` |

## Running Locally

```bash
cp .env.example .env        # fill in required vars
docker compose up --build   # starts all 11 services
```

Required env vars: `DISCORD_BOT_TOKEN`, `DISCORD_APPLICATION_ID`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `GEMINI_API_KEY`, `LAVALINK_PASSWORD`, `SESSION_SECRET_KEY`.

Optional: `CLAUDE_API_KEY` + `CLOUD_PROVIDER=claude` to switch narration from Gemini to Claude.

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
- `GET /api/settings/channels` — fetch runtime channel map (used by Discord bot at startup)
- `WS /ws/telemetry` — live pipeline event stream
- `GET /web/*` — White Portal admin panel
  - `/web/settings` — runtime config: channel map, AI model selection, API keys
  - `POST /web/settings/general` — save general + AI settings
  - `POST /web/settings/channels/add` — add or update a channel map entry
  - `POST /web/settings/channels/delete` — remove a channel map entry

## Development Branch

Active work lives on `claude/api-payload-schemas-0jkYr`. All Claude Code sessions should develop on branches prefixed `claude/` and push only to the designated branch.


---

## Agent Rules

> Generated from `.ai-rules/rules/` by `sync-rules.sh`. Edit the source files, not this section.

## Branch Rules

- Always develop on a `claude/<description>-<session-id>` branch.
- Never push directly to `alpha`, `beta`, or `main`.
- PRs from `claude/*` branches must target `alpha`, never `main`.
- Branch names must be lowercase, hyphen-separated, and end with the session ID.
- The active development branch is recorded in `CLAUDE.md` under **Development Branch**.

## Pipeline Contract

- All inter-service data passes through Pydantic models in `orchestrator/schemas/payloads.py`.
  Never pass raw dicts between pipeline phases.
- **Ollama never narrates.** Phase 2 produces only `OllamaResolutionPayload`. Narrative prose is
  Phase 4 only (GMDirector → Gemini / Claude / SillyTavern).
- **Dice are backend-only.** The LLM requests a roll via `DiceRequest`; the backend generates the
  result. The model cannot influence dice outcomes.
- **NodeRouter for all Ollama calls.** Never hardcode an Ollama node URL. All calls go through
  `NodeRouter.get_ollama_client_for_role()`.
- `action_log` rows are immutable. Retcons set `retconned=TRUE`; rows are never deleted.
- Sub-agent output is post-processed for brand filtering before reaching `NarrativeResponsePayload`.

## Naming and Branding Rules

- **In-world names must come from ingested PDF rulebooks** retrieved via RAG from ChromaDB.
  Names that appear in the active campaign's rulebooks are allowed in generated narrative.
- **Real-world brand names are blocked** unless the name appears in both the real world AND in an
  ingested rulebook (e.g. a licensed product that canonically uses real brand names).
  When in doubt, use a generic equivalent — never invent a real-world brand reference.
- The sub-agent post-processor applies brand filtering to all `SubAgentResult` output.
  If filtering fails after retry, `brand_violation=True` is set — do not suppress this flag.
- Do not introduce new proper nouns, weapon names, faction names, or lore terms that are not
  sourced from the campaign's rulebooks or the `story_facts` / `story_entities` tables.
- NPC names, location names, and item names generated during narration must be consistent with
  the active world's `narrative_tone` and genre (read from `WorldRegistry`).

## Database Rules

- New migrations go in `db/migrations/0NN_<snake_case>.sql`. The `NN` must be the next sequential
  number. **Never modify an existing migration file.**
- Schema changes require a migration. Never ALTER TABLE in application code.
- `global_settings` / `system_settings` seeds belong in the migration that introduces the feature.
- Always use asyncpg via `DatabaseService` — never a raw connection string in application code.
- New env vars declared in `orchestrator/config.py` must also appear in `.env.example`.

## Docker and Service Rules

- Service names follow the `aetheris_*` network convention defined in `docker-compose.yml`.
- New services must join `aetheris_net`. Services needing persistent storage also join `aetheris_store`.
- Health checks are required for every new service added to `docker-compose.yml`.
- Never hardcode ports in application code — read from environment variables.
- External services (SillyTavern, custom Ollama nodes, remote APIs) are configured via
  `system_settings` and are **never** added to `docker-compose.yml`.
- Compose override files for each tier (`compose.alpha.yml`, `compose.beta.yml`, `compose.prod.yml`)
  layer on top of the base `docker-compose.yml` — never duplicate full service definitions.

## Code Quality Rules

- All new Python files must pass `ruff check` with zero errors.
- No `print()` statements — use `logging.getLogger(__name__)`.
- No bare `except:` clauses — always catch a specific exception type.
- Type hints required on all public function signatures.
- Async functions must use `await` for all I/O — no blocking calls in coroutines.
- Do not add backwards-compatibility shims for removed code. Delete cleanly.
- Comments only where the WHY is non-obvious — do not comment what the code does.
- New services must be exported from `orchestrator/services/__init__.py`.

## Governance File Maintenance

When completing a significant task, update in the same commit:
- `.github/TODO.md` — mark completed items `[x]`, add newly discovered items
- `.github/PLANNING.md` — record architectural decisions made and handoff notes

After any change to the rule files under `.ai-rules/rules/`, run:
  `bash .ai-rules/scripts/sync-rules.sh`

This regenerates `.github/copilot-instructions.md` and `CLAUDE.md` from the modular sources.
Commit the regenerated files alongside the rule change — never edit the targets directly.


# Ironclad GM

**Self-hosted, multi-node AI Game Master for tabletop RPGs on Discord.**

Ironclad GM is a four-phase pipeline that turns free-text player actions into mechanically-resolved, narratively-rich responses — complete with dice rolls, stat tracking, ambient audio, NPC voice acting, and living Discord immersion features. It runs entirely on your own hardware (local Ollama nodes) with optional cloud fallback (Google Gemini).

---

## Architecture Overview

```
Discord ──► Orchestrator (FastAPI) ──► Discord
               │
     ┌─────────┼─────────┐
     ▼         ▼         ▼
  Phase 1   Phase 2   Phase 3   Phase 4
 Ingestion  Adjudic.  State    Narration
             (Ollama)  Commit   (GM Director)
                      (Postgres)  │
                                  ├── Tier 1: Storyteller (Gemini or auto-promoted Ollama)
                                  └── Tier 2: Sub-Agents (actor/scribe Ollama nodes)
```

### Four-Phase Pipeline

| Phase | Name | Engine | Purpose |
|-------|------|--------|---------|
| 1 | **Ingestion** | PostgreSQL + ChromaDB | Assemble character state, inventory, vehicle context, and retrieved rulebook chunks |
| 2 | **Adjudication** | Ollama (local LLM) | Mechanical resolution — dice rolls, stat deltas, outcome determination. No narrative. |
| 3 | **State Commit** | PostgreSQL + Redis | Atomic DB writes, CSV sync broadcast, session cache update |
| 4 | **Narration** | GM Director (two-tier) | Planning pass → sub-agent dispatch → synthesis → immersion layer |

### Hybrid AI Mesh

Multiple Ollama nodes are registered in the `node_registry` table, each tagged with roles:

| Role | Purpose |
|------|---------|
| `adjudication` | Mechanical resolution (Phase 2) |
| `actor` | NPC dialogue, combat flavour text |
| `scribe` | Environmental descriptions, item descriptions |
| `narrative` | General narrative fallback |
| `vision` | Image analysis (future) |

Nodes are health-checked every 30 seconds. TTFT (Time-to-First-Token) is measured via heartbeat prompts, enabling automatic storyteller promotion when cloud is toggled off.

---

## Implemented Features

### Task 1 — Core Pipeline & Data Model

- System-agnostic character sheets (JSONB stats)
- True-RNG dice injection — the backend owns all rolls, the LLM cannot override
- Immutable action log for full session replay
- Vehicle/subsystem mechanics with hull integrity and crew assignments
- RAG-powered rulebook retrieval (ChromaDB vector store)
- PDF rulebook ingestion with background processing
- CSV sync worker for spreadsheet exports
- Media asset proxy for image/audio serving

### Task 2 — Real-Time Latency Benchmarking & Auto-Promotion

- **TTFT Measurement**: Every 30 seconds, each Ollama node receives a heartbeat prompt. Time-to-first-token is measured and persisted to `node_registry.latency_ms`.
- **Auto-Promotion Protocol**: When the Cloud Storyteller toggle is OFF, the node with the lowest TTFT is automatically selected as the Tier 1 Storyteller for that turn.
- **Fallback Chain**: Preferred role → `narrative` fallback → any enabled Ollama → Gemini cloud (if enabled).
- **Health Loop**: Status probe + TTFT heartbeat run concurrently per node via `asyncio.gather`.

### Task 3 — Two-Tier Storyteller & Sub-Agent Orchestration

**GM Director** (Tier 1) orchestrates the full narration pipeline:

1. **Storyteller Selection** — Cloud toggle check → auto-promoted Ollama (by TTFT) → Gemini fallback
2. **Story Memory Retrieval** — Established world facts from ChromaDB, injected as immutable canon
3. **Planning Pass** — JSON-constrained output: which NPCs speak, what environments to describe, what items to detail
4. **Sub-Agent Dispatch** — Concurrent `asyncio.gather` to actor/scribe Ollama nodes (Tier 2)
5. **Synthesis Pass** — Storyteller weaves sub-agent outputs into cohesive prose
6. **Post-Processing** — Structural text filter strips headers/lists/markdown from narrative

**Immersion Rules** enforced on all output:
- No structural formatting (headers, bullet lists, numbered lists)
- No real-world brand names (Originality Lock — 150+ blocked terms with retry+strip)
- No unsolicited character sheet dumps (stat block only when changes exist)
- No fourth-wall breaks

**Originality Lock** (Brand Filter):
- After each sub-agent completes, output is checked against `BRAND_BLOCKLIST`
- Attempt 1-2: Correction instruction appended, prompt re-sent
- Final attempt: Offending terms replaced with `[???]` and flagged for audit

### Task 4 — Living Discord Immersion Layer

Four immersion systems that make the Discord server feel alive:

#### Paranoia Whisper System
Private DMs sent to the acting player with secret GM insights — what their character notices that nobody else in the scene would catch. Generated in parallel with synthesis (zero added latency via `asyncio.gather`).

#### Ephemeral Combat Threads (Ghost Sheet)
- Combat actions automatically open a thread on the narrative message
- Mechanical details (dice rolls, damage, inventory changes, citations) posted inside the thread — never in the main channel
- Thread auto-archives and locks when the encounter ends
- Thread state tracked per-channel in the Discord bot

#### Voice Channel Puppeteering
- **Ambient Audio**: Loops environmental audio (tavern chatter, dungeon ambience, combat tension) at 25% volume. Skips restart if the same track is already playing.
- **NPC Voice Acting**: Each Ollama node has a unique `voice_id` (edge-tts voice profile). NPC dialogue is spoken aloud with the voice of the node that generated it. TTS audio is cached by `SHA-256(voice_id + text)`.
- **Playback**: Ambient pauses during TTS speech, resumes after. Sequential cue playback with dramatic pauses between speakers.

#### Channel Manipulation
Permission-based player location simulation. When narrative warrants it (capture, escape, rescue), the bot manipulates channel permissions to move players between dungeon/prison/hospital/main channels.

---

## Service Architecture

```
docker-compose.yml
├── ironclad-orchestrator    FastAPI pipeline server          :8000
├── ironclad-discord         Discord bot (discord.py + voice)
├── ironclad-db              PostgreSQL 16
├── ironclad-cache           Redis 7
├── ironclad-ollama          Ollama LLM server
├── ironclad-chroma          ChromaDB vector store
├── ironclad-csv-sync        CSV export worker
├── media-asset-proxy        Static asset server              :8001
└── lavalink-audio           Lavalink audio engine            :2333
```

**Network Isolation:**
- `ironclad-compute` — External-facing services (orchestrator, Discord, Ollama, ChromaDB, media proxy, Lavalink)
- `ironclad-storage` — Internal only (PostgreSQL, Redis). No direct external access.

---

## Project Structure

```
RPG-Bot/
├── docker-compose.yml
├── .env.example
│
├── db/
│   ├── schema.sql                          # Core tables: campaigns, characters, inventories, action_log, sessions
│   └── migrations/
│       ├── 002_story_memory.sql            # Story facts + entity tracking
│       ├── 003_vehicles_and_nodes.sql      # Vehicles, subsystems, node_registry
│       ├── 004_node_roles_and_settings.sql # Role tags, global_settings
│       ├── 005_node_latency.sql            # TTFT benchmarking columns
│       ├── 006_node_voice_profile.sql      # Per-node voice_id for TTS
│       ├── 007_async_session_features.sql  # downtime_tasks, player_presence, retcon_log
│       ├── 008_admin_backchannel.sql       # gm_directives, fair_play_mode seed
│       ├── 009_admin_auth.sql              # admin_accounts (first-boot setup lock)
│       ├── 010_rolling_vault.sql           # rolling_vault sliding context window
│       └── 011_settings_channels.sql       # system_settings seed (channel map, model names)
│
├── orchestrator/
│   ├── main.py                             # FastAPI app, pipeline wiring, API endpoints
│   ├── config.py                           # Pydantic settings from environment
│   ├── Dockerfile
│   ├── requirements.txt
│   │
│   ├── pipeline/
│   │   ├── ingestion.py                    # Phase 1: Context assembly
│   │   ├── adjudication.py                 # Phase 2: Mechanical resolution (Ollama)
│   │   ├── state_commit.py                 # Phase 3: PostgreSQL + Redis commit
│   │   └── narration.py                    # Phase 4: Delegates to GM Director
│   │
│   ├── prompts/
│   │   ├── guardrails.py                   # Base prompt guardrails
│   │   ├── gm_prompts.py                   # GM system/planning/synthesis prompts, brand blocklist
│   │   └── immersion_prompts.py            # Whisper prompts, ambient map, combat/channel detection
│   │
│   ├── schemas/
│   │   └── payloads.py                     # All Pydantic models (pipeline + GM + immersion)
│   │
│   ├── services/
│   │   ├── database.py                     # asyncpg database service
│   │   ├── cache.py                        # Redis session/lock service
│   │   ├── ollama_client.py                # Ollama HTTP client (with node_name, voice_id)
│   │   ├── gemini_client.py                # Google Gemini API client
│   │   ├── node_router.py                  # Multi-node mesh, TTFT benchmarking, auto-promotion
│   │   ├── gm_director.py                  # Tier 1 GM: planning → dispatch → synthesis
│   │   ├── sub_agent_dispatcher.py         # Tier 2: concurrent sub-agent execution + brand filter
│   │   ├── story_memory.py                 # ChromaDB-backed world fact store
│   │   ├── rag_service.py                  # Rulebook vector retrieval
│   │   └── pdf_processor.py               # PDF ingestion pipeline
│   │
│   ├── routers/
│   │   └── web_ui.py                       # Web admin routes (Rule Forge)
│   │
│   └── templates/                          # Jinja2 HTML templates
│       ├── base.html
│       ├── dashboard.html
│       ├── rules.html
│       ├── lore.html
│       └── log.html
│
├── discord-bot/
│   ├── bot.py                              # Discord listener, embed delivery, immersion handlers
│   ├── voice_manager.py                    # Voice channel audio: ambient + TTS playback
│   ├── Dockerfile
│   └── requirements.txt
│
├── csv-sync/
│   ├── worker.py                           # Redis pub/sub → CSV export worker
│   ├── Dockerfile
│   └── requirements.txt
│
├── media-proxy/
│   ├── server.py                           # Static file server for images/audio
│   ├── Dockerfile
│   └── requirements.txt
│
└── lavalink/
    └── application.yml                     # Lavalink audio engine config
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/action` | Process a player action through the full four-phase pipeline |
| `POST` | `/session` | Create or refresh a player session |
| `POST` | `/api/rulebook/ingest` | Upload and ingest a PDF rulebook (background job) |
| `GET` | `/api/rulebook/status/{job_id}` | Poll PDF ingestion progress |
| `GET` | `/api/campaign/active` | Get the active campaign for a guild |
| `GET` | `/health` | Health check |
| `GET` | `/web/*` | Web admin panel (Rule Forge) |

---

## Data Flow: Player Action → Response

```
1. Player types "I attack the goblin" in Discord
2. Discord bot constructs IntentPayload, POSTs to /action
3. Phase 1 (Ingestion): Loads character sheet, inventory, rulebook chunks from DB + ChromaDB
4. Phase 2 (Adjudication): Ollama resolves mechanics — dice roll, AC check, damage calc
5. Phase 3 (State Commit): HP delta written to PostgreSQL, Redis cache updated, CSV sync notified
6. Phase 4 (Narration):
   a. GM Director selects storyteller (Gemini or fastest Ollama)
   b. Planning pass produces sub-task list (NPC reactions, environment, combat flavour)
   c. Sub-agents execute concurrently on actor/scribe Ollama nodes
   d. Storyteller synthesizes sub-agent outputs into prose
   e. Whisper generated in parallel (what the player's instincts notice)
   f. Structural text filter strips formatting artifacts
   g. Story facts extracted and persisted to memory
7. Discord bot receives NarrativeResponsePayload:
   - Posts narrative embed to main channel
   - DMs paranoia whisper to player
   - Opens/updates combat thread with mechanical details
   - Plays ambient audio + NPC voice lines in voice channel
   - Adjusts channel permissions if location change warranted
```

---

## Payload Schemas

All inter-service payloads are defined as Pydantic models in `orchestrator/schemas/payloads.py`:

| Schema | Phase | Purpose |
|--------|-------|---------|
| `IntentPayload` | Input | Discord → Orchestrator entry point |
| `ContextAssemblyPayload` | 1 | Character + inventory + rules + vehicles |
| `OllamaResolutionPayload` | 2 | Dice rolls, stat deltas, outcome, citations |
| `StateCommitPayload` | 3 | Pre/post state snapshot for audit |
| `NarrativeResponsePayload` | 4 | Prose + whisper + thread + audio + channel directives |
| `SubAgentTask` | 4 (internal) | Delegation unit for Tier 2 sub-agents |
| `SubAgentResult` | 4 (internal) | Sub-agent output with brand violation flag |
| `GMPlanResult` | 4 (internal) | Planning pass output (sub-tasks + direct elements) |
| `PipelineResult` | Audit | Aggregate of all four phases for action_log |

---

## Database Schema

PostgreSQL with JSONB columns for system-agnostic flexibility:

| Table | Purpose |
|-------|---------|
| `campaigns` | Campaign metadata, active system (D&D 5e, Cyberpunk 2020, etc.) |
| `characters` | Player characters with JSONB stats |
| `inventories` | Per-character item stacks (JSONB item_data) |
| `rule_registry` | Active rulebook modules per campaign (PDF, JSON, vector) |
| `action_log` | Immutable audit trail of every action + resolution |
| `sessions` | Active interaction sessions (mirrors Redis TTL) |
| `story_facts` | Established world facts for narrative continuity |
| `story_entities` | Named entities (NPCs, locations, events) |
| `vehicles` | Vehicle/asset state with hull integrity |
| `vehicle_subsystems` | Individual subsystems (weapons, shields, engines) |
| `node_registry` | Ollama node mesh: host, model, roles, latency_ms, voice_id |
| `global_settings` | Key-value config (cloud_storyteller toggle, etc.) |

---

## Setup & Deployment

### Prerequisites

- Docker & Docker Compose
- A Discord bot token with Message Content, Voice, and Members intents
- Google Gemini API key (for cloud storyteller — optional if using local-only mode)
- At least one machine capable of running Ollama models

### Quick Start

```bash
# 1. Clone the repository
git clone <repo-url> && cd RPG-Bot

# 2. Configure environment
cp .env.example .env
# Edit .env with your credentials (see Environment Variables below)

# 3. Launch all services
docker compose up -d

# 4. Run database migrations
docker compose exec ironclad-db psql -U ironclad -d ironclad \
  -f /docker-entrypoint-initdb.d/001_schema.sql
# Then run each migration in order:
# migrations 002 through 011

# 5. Pull an Ollama model
docker compose exec ironclad-ollama ollama pull mistral:7b-instruct

# 6. Register your Ollama node(s) in node_registry
# (via the web admin panel at http://localhost:8000/web/ or direct SQL)

# 7. Invite the Discord bot to your server
# The bot needs: Send Messages, Manage Threads, Manage Channels,
# Connect (voice), Speak (voice), Send Messages in Threads
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | — | Discord bot token |
| `DISCORD_APPLICATION_ID` | Yes | — | Discord application ID |
| `POSTGRES_PASSWORD` | Yes | — | PostgreSQL password |
| `REDIS_PASSWORD` | Yes | — | Redis password |
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `LAVALINK_PASSWORD` | Yes | — | Lavalink server password |
| `SESSION_SECRET_KEY` | Yes | — | Secret key for session middleware |
| `POSTGRES_DB` | No | `ironclad` | Database name |
| `POSTGRES_USER` | No | `ironclad` | Database user |
| `GEMINI_MODEL` | No | `gemini-1.5-pro` | Gemini model identifier |
| `OLLAMA_MODEL` | No | `mistral:7b-instruct` | Default Ollama model |
| `LOG_LEVEL` | No | `INFO` | Python logging level |
| `SESSION_TTL_SECONDS` | No | `3600` | Session expiry (seconds) |

**Discord bot voice environment (set in discord-bot container):**

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIO_DIR` | `/app/audio` | Directory containing ambient audio .mp3 files |
| `TTS_CACHE_DIR` | `/tmp/ironclad_tts` | TTS audio cache directory |
| `AMBIENT_VOLUME` | `0.25` | Ambient audio volume (0.0–1.0) |
| `TTS_VOLUME` | `0.90` | TTS voice volume (0.0–1.0) |

> **Note:** Discord channel IDs (dungeon, prison, hospital, main, etc.) are no longer set via environment variables. They are managed at runtime through **White Portal → Settings → Channel Map**.

### Adding Ambient Audio

Place `.mp3` files in the audio directory mounted to the Discord bot container:

```
/app/audio/
  combat_tension.mp3
  tavern_chatter.mp3
  dungeon_ambience.mp3
  workshop_sounds.mp3
```

New tracks can be added freely — the ambient audio key in the narrative payload maps to the filename (without extension).

### Multi-Node Ollama Mesh

To add additional Ollama nodes (e.g., on different machines):

1. Register the node in `node_registry` with its host URL, model, and roles
2. Optionally assign a `voice_id` for unique TTS voice identity (e.g., `en-US-GuyNeural`, `en-GB-RyanNeural`)
3. The node router will automatically discover it, begin TTFT benchmarking, and include it in the dispatch pool

### GPU Acceleration

Uncomment the `deploy` section in `docker-compose.yml` under `ironclad-ollama` to enable NVIDIA GPU passthrough:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```

---

## Discord Commands

| Command | Description |
|---------|-------------|
| `/act <action>` | Perform a narrative action (triggers full pipeline) |
| Free-text in game channel | Same as `/act` — natural language input |
| `/rulebook <pdf>` | Upload a PDF rulebook for ingestion |
| `/worlds` | List all discovered worlds with tone and tags |
| `/switch_world <name>` | Bind the campaign to a world (creates it if new) |

---

## Web Admin Panel

Access the White Portal at `http://localhost:8000/web/`:

- **Dashboard** — Campaign overview and system status
- **Rules** — Manage active rulebook modules per campaign
- **Lore** — Browse and manage story facts / world canon
- **Log** — Action log viewer with full pipeline replay
- **Nodes** — AI node registry and connection dashboard
- **Backchannel** — White Portal admin directive interface (OOC GM commands)
- **Settings** — Runtime configuration: channel map, AI model selection, API keys

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Orchestrator | Python 3.12, FastAPI, Pydantic, asyncpg |
| Discord Bot | discord.py (with voice), edge-tts |
| Database | PostgreSQL 16 (JSONB, GIN indexes) |
| Cache | Redis 7 (sessions, locks, pub/sub) |
| Vector Store | ChromaDB (RAG rulebook retrieval, story memory) |
| Local LLM | Ollama (Mistral, Llama, etc.) |
| Cloud LLM | Google Gemini API |
| Audio | Lavalink 4, FFmpeg, edge-tts |
| Deployment | Docker Compose with isolated networks |

---

## License

This project is for personal/self-hosted use.

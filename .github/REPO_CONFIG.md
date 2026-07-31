# Ironclad GM — Repository Configuration

Technical reference for tooling, commands, and monitored files.

---

## Runtime

| Component | Version / Value |
|-----------|----------------|
| Python | 3.11 (orchestrator + discord-bot) |
| Node.js | Not used |
| Package manager | pip (per-service `requirements.txt`) |
| Container runtime | Docker + Docker Compose v2 |
| Database | PostgreSQL 16 |
| Cache | Redis 7 |
| Vector store | ChromaDB |
| Local LLM | Ollama (multi-node mesh) |
| Cloud LLM | Gemini 1.5 Pro / Claude Sonnet 4.6 |
| Audio | Lavalink + wavelink / Lyria 3 |

---

## Service Map (docker-compose.yml)

| Service name | Role | Port(s) |
|-------------|------|---------|
| `scribe` | FastAPI orchestrator | 8000 |
| `brain` | Ollama LLM engine | 11434 |
| `pulse` | Health sentinel (Flask) | 58291 |
| `janitor` | Backup + prune (Alpine) | — |
| `lavalink` | Audio streaming engine | 2333 |
| `media-proxy` | Static asset server | 8001 |
| `chroma` | ChromaDB vector store | 8000 (internal) |
| `ironclad-db` | PostgreSQL 16 | 5432 |
| `ironclad-cache` | Redis 7 | 6379 |
| `discord-bot` | Discord listener | — |
| `comfyui` | Image generation (optional) | 8188 |

Networks: `aetheris_net` (application), `aetheris_store` (persistence)

---

## Key Commands

```bash
# Start full stack
docker compose up --build

# Start with tier overlay
docker compose -f docker-compose.yml -f compose.alpha.yml up --build

# Run orchestrator tests
docker compose run --rm scribe pytest

# Lint Python
docker compose run --rm scribe ruff check orchestrator/ discord-bot/

# Apply a migration manually
docker compose exec ironclad-db psql -U ironclad -d ironclad \
  -f /migrations/0NN_<name>.sql

# Tail orchestrator logs
docker compose logs -f scribe

# Restart a single service without full rebuild
docker compose restart scribe

# Full wipe and rebuild
docker compose down -v && docker compose up --build
```

---

## Required Environment Variables

Minimum set to start the stack (see `.env.example` for full list):

| Variable | Service | Notes |
|----------|---------|-------|
| `DISCORD_BOT_TOKEN` | discord-bot | Required |
| `DISCORD_APPLICATION_ID` | discord-bot | Required |
| `POSTGRES_PASSWORD` | ironclad-db + scribe | Required |
| `REDIS_PASSWORD` | ironclad-cache + scribe | Required |
| `GEMINI_API_KEY` | scribe | Required (default storyteller) |
| `LAVALINK_PASSWORD` | lavalink + discord-bot | Required |
| `SESSION_SECRET_KEY` | scribe | Required (White Portal auth) |
| `CLAUDE_API_KEY` | scribe | Optional — set `CLOUD_PROVIDER=claude` to activate |
| `ELEVENLABS_API_KEY` | scribe | Optional — SFX + TTS |
| `OPENAI_API_KEY` | scribe | Optional — DALL-E 3 + TTS |
| `SILLYTAVERN_URL` | scribe | Optional — external ST instance |

---

## Monitored Files (changes require PLANNING.md / TODO.md updates)

- `orchestrator/schemas/payloads.py` — pipeline contract; any change affects all phases
- `db/migrations/` — irreversible; new files only, never edit existing
- `docker-compose.yml` — service topology; changes affect all tiers
- `orchestrator/config.py` — all env vars declared here; update `.env.example` in same PR
- `orchestrator/services/__init__.py` — service exports; keep in sync with new service files
- `orchestrator/prompts/gm_prompts.py` — GM behaviour; changes affect live narrative quality
- `discord-bot/requirements.txt` — bot dependencies; test after every change

---

## Source Layout

```
orchestrator/          FastAPI pipeline engine (runs as 'scribe' container)
  config.py            Pydantic settings — all env vars
  schemas/payloads.py  Single source of truth for all inter-service contracts
  pipeline/            4-phase pipeline: ingestion → adjudication → state_commit → narration
  services/            All stateful services (DB, cache, LLM clients, GM director, etc.)
  prompts/             GM system prompts, immersion rules, guardrails
  routers/             FastAPI route modules (web_ui, auth)
  templates/           White Portal Jinja2 templates

discord-bot/           Discord listener + voice manager (runs as 'discord-bot' container)
  bot.py               Slash commands, event handlers, narrative delivery
  voice_manager.py     Voice channel audio: ambient, TTS, music, SFX

db/
  schema.sql           Initial schema (used by initdb — do not modify)
  migrations/          0NN_*.sql — run manually after deploy in sequence

health-sentinel/       Flask sidecar — reports busy/ok status on :58291
janitor/               Alpine container — GFS backup rotation + media prune
media-proxy/           Static asset server on :8001
lavalink/              Audio engine config
```

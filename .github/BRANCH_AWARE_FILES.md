# Ironclad GM — Branch-Aware Files

Files whose **content** (not just existence) must be reviewed and potentially updated when
promoting between tiers (`claude/* → alpha → beta → main`).

For each promotion, go through every item in the relevant section and confirm it is correct
for the target branch before merging.

---

## All Promotions (any tier change)

| File | What to check |
|------|--------------|
| `.github/TODO.md` | Completed items marked `[x]`, no stale `[-]` in-progress items |
| `.github/PLANNING.md` | Handoff notes updated, decision log current |
| `db/migrations/` | All new `0NN_*.sql` files documented in the PR description |
| `.env.example` | Any new env vars from `orchestrator/config.py` are present |
| `orchestrator/schemas/payloads.py` | No uncommitted schema changes that would break consumers |

---

## `claude/* → alpha`

| File | What to check |
|------|--------------|
| `docker-compose.yml` | Service definitions compile; no syntax errors |
| `orchestrator/config.py` | New settings have sensible defaults for a fresh alpha deploy |
| `discord-bot/requirements.txt` | All new deps pinned to compatible versions |
| `orchestrator/services/__init__.py` | All new services exported |
| `orchestrator/main.py` | New services instantiated in lifespan; new endpoints registered |
| `db/migrations/0NN_*.sql` | Apply cleanly to a fresh DB in sequence |

---

## `alpha → beta`

| File | What to check |
|------|--------------|
| `compose.alpha.yml` | Override file exists and targets correct image tags |
| `orchestrator/prompts/gm_prompts.py` | No debug / placeholder prompt text |
| `orchestrator/templates/*.html` | All referenced template variables exist in route handlers |
| `discord-bot/bot.py` | No `ephemeral=False` on sensitive commands; no debug logging of tokens |
| `db/schema.sql` | Does NOT need updating (migrations only) — confirm no accidental edits |

---

## `beta → main`

| File | What to check |
|------|--------------|
| `compose.beta.yml` → `compose.prod.yml` | Prod override has `restart: always`, correct resource limits |
| `docker-compose.yml` | Image tags pinned (no `latest`) for all services |
| `orchestrator/config.py` | `log_level` defaults to `WARNING` for prod |
| `health-sentinel/` | Sentinel config points to prod Redis |
| `lavalink/application.yml` | Lavalink password matches prod `LAVALINK_PASSWORD` env var |
| `.env.example` | All required vars documented; no secrets committed |
| `CLAUDE.md` | Active development branch reference updated to reflect new `claude/*` session |

---

## Files That Should NEVER Change Between Tiers

These are pinned — if a PR modifies them without a clear reason, flag it in review:

- `db/migrations/0NN_*.sql` — existing migration files are immutable
- `orchestrator/services/reality_wall.py` — SQLite vault schema is frozen
- `orchestrator/routers/auth_router.py` — auth flow changes require security review

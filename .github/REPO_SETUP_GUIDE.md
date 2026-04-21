# Ironclad GM — Repository Setup Guide

Adapted from the Zerotierone-moon governance model. Establishes 4-tier branch governance,
CI enforcement, and multi-agent coordination rules for this self-hosted AI Game Master stack.

---

## Branch Tier Structure

```
claude/* (feature)  →  alpha  →  beta  →  main
```

| Branch | Purpose | Audience |
|--------|---------|----------|
| `claude/*` | Active development — all Claude Code sessions land here | Developer / AI agents |
| `alpha` | Integration — full 11-service stack smoke-tested | Dev self-test |
| `beta` | Staging — load-tested, all migrations applied | Pre-release review |
| `main` | Production — live deployment | End users |

**Rule:** PRs always target `alpha`. Direct pushes to `alpha`, `beta`, and `main` are blocked.
`claude/*` is the only branch agents write to.

---

## Docker Compose Tier Targets

Each tier has a compose override file layered on top of the base `docker-compose.yml`:

| Tier | Command |
|------|---------|
| Local dev | `docker compose up --build` |
| Alpha | `docker compose -f docker-compose.yml -f compose.alpha.yml up --build` |
| Beta | `docker compose -f docker-compose.yml -f compose.beta.yml up --build` |
| Main (prod) | `docker compose -f docker-compose.yml -f compose.prod.yml up --build` |

Override files control image tags, replica counts, and resource limits.
The base `docker-compose.yml` is always the source of truth for service definitions.

---

## Governance Files Required

All of these must be present for CI to pass. Their purpose:

| File | Purpose |
|------|---------|
| `.github/copilot-instructions.md` | Agent behaviour rules — what AI agents must and must not do |
| `.github/REPO_CONFIG.md` | Runtime, package manager, test commands, monitored files |
| `.github/TODO.md` | Active task tracking — one in-progress task per agent at a time |
| `.github/PLANNING.md` | Initiatives, architectural decisions, handoff notes |
| `.github/BRANCH_AWARE_FILES.md` | Files whose content must be updated during branch promotions |
| `.github/REPO_SETUP_GUIDE.md` | This document |

---

## Branch Protection Rules

Apply to `alpha`, `beta`, and `main`:

- Require pull request before merging
- Require at least 1 approving review
- Require all status checks to pass before merging
- Require branches to be up to date before merging
- Do not allow bypassing the above settings
- Restrict direct pushes (no force push to protected branches)

`claude/*` branches are unprotected — agents push freely.

---

## CI Enforcement (`.github/workflows/ci.yml`)

Runs on every PR targeting `alpha`, `beta`, or `main`. Validates:

1. All 6 governance files are present
2. Python syntax — `python -m py_compile` on `orchestrator/` and `discord-bot/`
3. Ruff lint — `ruff check orchestrator/ discord-bot/`
4. Migration naming convention — all files under `db/migrations/` match `0NN_*.sql`
5. Bash syntax — `bash -n` on any `.sh` scripts
6. Schema contract — `orchestrator/schemas/payloads.py` imports cleanly

---

## Promotion Checklist

When promoting `alpha → beta` or `beta → main`:

- [ ] All CI checks green on the source branch
- [ ] `db/migrations/` — any new migrations documented in PR description
- [ ] `.env.example` updated if new env vars added
- [ ] `BRANCH_AWARE_FILES.md` reviewed — all listed files updated for new tier
- [ ] `PLANNING.md` updated with promotion decision and date
- [ ] `TODO.md` reflects any newly completed or newly opened items
- [ ] Docker override file for target tier exists and tested
- [ ] No hardcoded `claude/*` branch references in source files

---

## Available Docker Operations

```bash
# Standard start (local dev)
docker compose up --build

# Unattended start (CI/CD)
docker compose up --build -d

# Apply new migrations
docker compose run --rm scribe python -m orchestrator.db.migrate

# Restart a single service
docker compose restart scribe

# Full teardown and rebuild
docker compose down -v && docker compose up --build
```

---

## Naming Conventions

- Docker networks: `aetheris_net`, `aetheris_store`
- Docker service names: `scribe` (orchestrator), `brain` (Ollama), `pulse` (health sentinel),
  `janitor`, `lavalink`, `media-proxy`, `chroma`, `ironclad-db`, `ironclad-cache`
- Migration files: `db/migrations/0NN_<snake_case_description>.sql`
- Claude Code session branches: `claude/<description>-<session-id>`
- Compose overrides: `compose.<tier>.yml`

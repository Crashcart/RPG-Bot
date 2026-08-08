# Docker Image Optimization — Solution Summary

**Branch:** `claude/chore/docker-optimization`
**Guided by:** `.github/docker-optimization.md`

## Changes

Five Python services were updated with multi-stage builds and `.dockerignore` files.
The `janitor/` service already uses `alpine:3.20` — no changes needed there.

### Multi-stage builds (5 services)

| Service | Base (before) | Runtime (after) | Builder |
|---------|--------------|-----------------|---------|
| `orchestrator` | `python:3.12-slim` | `python:3.12-slim` | `python:3.12-slim` + `build-essential` |
| `discord-bot` | `python:3.12-slim` | `python:3.12-slim` | `python:3.12-slim` + `build-essential` |
| `health-sentinel` | `python:3.12-slim` | **`python:3.12-alpine`** | `python:3.12-alpine` |
| `media-proxy` | `python:3.12-slim` | `python:3.12-slim` | `python:3.12-slim` + `build-essential` |
| `csv-sync` | `python:3.12-slim` | `python:3.12-slim` | `python:3.12-slim` + `build-essential` |

**Pattern applied** (per `.github/docker-optimization.md` § Python example):
```dockerfile
FROM python:3.12-slim AS builder
RUN apt-get install -y --no-install-recommends build-essential ...
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-slim
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
COPY . .
```

### `.dockerignore` files (5 services)

Added to: `orchestrator/`, `discord-bot/`, `health-sentinel/`, `media-proxy/`, `csv-sync/`

Excludes: `__pycache__/`, `*.pyc`, `.git/`, `.env*`, `tests/`, `.pytest_cache/`, `*.md`, build artifacts.

### Why `health-sentinel` uses Alpine

`health-sentinel` depends only on `flask`, `redis`, and `gunicorn` — all pure-Python wheels
with no native compilation required. Alpine (`python:3.12-alpine`, ~22 MB) vs slim
(`python:3.12-slim`, ~130 MB) gives ~85% base-image reduction for this service.

### Notes on `orchestrator`

`orchestrator` keeps `curl` in the **runtime** stage (not the builder) because the
`docker-compose.yml` health-check uses `curl http://localhost:8000/health`. Build
tools (`build-essential`) are builder-only; `asyncpg`, `chromadb`, and `pymupdf` wheels
may require compilation on non-amd64 targets.

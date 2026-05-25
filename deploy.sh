#!/usr/bin/env bash
# =============================================================================
# Ironclad GM — Pre-flight deployment guard
# Usage:
#   ./deploy.sh            — validate then deploy
#   ./deploy.sh --force    — tear down existing stack, then deploy
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; }
info()  { echo -e "${CYAN}[→]${NC} $*"; }

# ── Load environment ──────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
    error ".env not found. Run ./install.sh first, or: cp .env.example .env"
    exit 1
fi
set -a; source .env; set +a

PROJECT_PREFIX="${PROJECT_PREFIX:-aetheris}"
APP_HOST_PORT="${APP_HOST_PORT:-8000}"
MEDIA_PROXY_PORT="${MEDIA_PROXY_PORT:-8001}"
PULSE_PORT="${PULSE_PORT:-58291}"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║          Ironclad GM — Pre-flight Check              ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
info "Stack prefix: ${PROJECT_PREFIX}"

CONFLICTS=0

# ── 1. Container conflict check ───────────────────────────────────────────────
info "Checking for running containers..."
MANAGED_CONTAINERS=(
    "${PROJECT_PREFIX}-scribe"
    "${PROJECT_PREFIX}-discord"
    "${PROJECT_PREFIX}-brain"
    "${PROJECT_PREFIX}-janitor"
    "${PROJECT_PREFIX}-db"
    "${PROJECT_PREFIX}-cache"
    "${PROJECT_PREFIX}-chroma"
    "${PROJECT_PREFIX}-csv-sync"
    "${PROJECT_PREFIX}-media"
    "${PROJECT_PREFIX}-pulse"
    "${PROJECT_PREFIX}-lavalink"
)
for container in "${MANAGED_CONTAINERS[@]}"; do
    if docker ps -q -f "name=^/${container}$" 2>/dev/null | grep -q .; then
        error "  Container conflict: ${container} is already running."
        error "    Stop it first: docker stop ${container} && docker rm ${container}"
        CONFLICTS=$((CONFLICTS + 1))
    fi
done
[[ $CONFLICTS -eq 0 ]] && log "No container conflicts detected."

# ── 2. Host port conflict check ───────────────────────────────────────────────
info "Checking host ports..."
declare -A CHECK_PORTS=(
    ["scribe (orchestrator)"]="${APP_HOST_PORT}"
    ["media-proxy"]="${MEDIA_PROXY_PORT}"
    ["pulse (health sentinel)"]="${PULSE_PORT}"
)
for service in "${!CHECK_PORTS[@]}"; do
    port="${CHECK_PORTS[$service]}"
    in_use=0
    if command -v ss &>/dev/null && ss -tuln 2>/dev/null | grep -q ":${port} "; then
        in_use=1
    elif command -v lsof &>/dev/null && lsof -iTCP:"${port}" -sTCP:LISTEN -n -P 2>/dev/null | grep -q LISTEN; then
        in_use=1
    fi
    if [[ $in_use -eq 1 ]]; then
        error "  Port conflict: :${port} is already in use (needed by ${service})."
        error "    Identify the process: ss -tlnp | grep :${port}"
        CONFLICTS=$((CONFLICTS + 1))
    fi
done
[[ $CONFLICTS -eq 0 ]] && log "No port conflicts detected."

# ── 3. Abort on conflicts ─────────────────────────────────────────────────────
if [[ $CONFLICTS -gt 0 ]]; then
    echo ""
    echo -e "${RED}${BOLD}Pre-flight failed: ${CONFLICTS} conflict(s) detected. Aborting.${NC}"
    echo "  Pass --force to tear down the existing stack and redeploy."
    exit 1
fi

log "Pre-flight passed — proceeding with deployment."
echo ""

# ── 4. --force: tear down before redeploy ────────────────────────────────────
FORCE=0
PASSTHROUGH=()
for arg in "$@"; do
    [[ "$arg" == "--force" ]] && FORCE=1 || PASSTHROUGH+=("$arg")
done

if [[ $FORCE -eq 1 ]]; then
    warn "--force: tearing down existing stack first..."
    if docker compose version &>/dev/null 2>&1; then
        docker compose down --remove-orphans
    else
        docker-compose down --remove-orphans
    fi
    echo ""
fi

# ── 5. Deploy ─────────────────────────────────────────────────────────────────
if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
else
    DC="docker-compose"
fi

info "Launching: ${DC} up -d --build"
$DC up -d --build "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"

echo ""
log "Stack '${PROJECT_PREFIX}' is up."
echo ""
echo "  Orchestrator:  http://localhost:${APP_HOST_PORT}/web/"
echo "  Health Pulse:  http://localhost:${PULSE_PORT}/"
echo "  Media Proxy:   http://localhost:${MEDIA_PROXY_PORT}/"
echo ""

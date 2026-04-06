#!/usr/bin/env bash
# =============================================================================
# Ironclad GM — Pre-Flight Deployment Wrapper  (TDR §2-B)
#
# Usage:
#   ./deploy.sh [--force] [-- <extra docker compose args>]
#
# Flags:
#   --force   Tear down any already-running containers under this PROJECT_PREFIX
#             before starting fresh (CI/CD Option 1 — TDR §4).
#
# Environment (loaded from .env automatically):
#   PROJECT_PREFIX        — naming prefix for all containers  (default: ironclad-gm)
#   APP_HOST_PORT         — orchestrator host port            (default: 8000)
#   MEDIA_PROXY_HOST_PORT — media-proxy host port             (default: 8001)
#   PULSE_HOST_PORT       — health-sentinel host port         (default: 58291)
#
# Exit codes:
#   0  — deployment started successfully
#   1  — pre-flight check failed (container conflict or port conflict)
#   2  — missing dependency (docker / docker compose)
# =============================================================================
set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
info()  { echo -e "${CYAN}[→]${NC} $*"; }

# ── Parse flags ───────────────────────────────────────────────────────────────
FORCE=false
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=true; shift ;;
        --)      shift; EXTRA_ARGS=("$@"); break ;;
        *)       EXTRA_ARGS+=("$1"); shift ;;
    esac
done

# ── Dependency checks ────────────────────────────────────────────────────────
command -v docker &>/dev/null || { echo -e "${RED}[✗]${NC} Docker is not installed." >&2; exit 2; }

if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo -e "${RED}[✗]${NC} Docker Compose not found." >&2
    exit 2
fi

# ── Load .env ────────────────────────────────────────────────────────────────
if [[ -f ".env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
else
    warn ".env not found — using defaults. Copy .env.example to .env and fill in values."
fi

PROJECT_PREFIX="${PROJECT_PREFIX:-ironclad-gm}"
APP_HOST_PORT="${APP_HOST_PORT:-8000}"
MEDIA_PROXY_HOST_PORT="${MEDIA_PROXY_HOST_PORT:-8001}"
PULSE_HOST_PORT="${PULSE_HOST_PORT:-58291}"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║     Ironclad GM — Pre-Flight Check                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
info "PROJECT_PREFIX  : ${PROJECT_PREFIX}"
info "APP_HOST_PORT   : ${APP_HOST_PORT}"
info "MEDIA_PROXY_PORT: ${MEDIA_PROXY_HOST_PORT}"
info "PULSE_PORT      : ${PULSE_HOST_PORT}"
echo ""

# ── Helper: check if a container name is already running ─────────────────────
_container_running() {
    local name="$1"
    [[ -n "$(docker ps -q -f "name=^${name}$" 2>/dev/null)" ]]
}

# ── Helper: check if a host port is already bound ────────────────────────────
_port_in_use() {
    local port="$1"
    if command -v ss &>/dev/null; then
        local ss_out
        ss_out=$(ss -tuln 2>/dev/null)
        grep -q ":${port} " <<< "$ss_out" || grep -q ":${port}$" <<< "$ss_out"
    elif command -v lsof &>/dev/null; then
        lsof -i ":${port}" -sTCP:LISTEN &>/dev/null
    else
        # Fallback: attempt a TCP connect; port 0 means we skip if no tools available
        warn "Neither 'ss' nor 'lsof' found — skipping port ${port} check."
        return 1
    fi
}

# =============================================================================
# Step 1: Container State Validation  (TDR §2-B-1)
# =============================================================================
info "Checking for running containers with prefix '${PROJECT_PREFIX}'..."

CONTAINERS=(
    "${PROJECT_PREFIX}-scribe"
    "${PROJECT_PREFIX}-discord"
    "${PROJECT_PREFIX}-brain"
    "${PROJECT_PREFIX}-db"
    "${PROJECT_PREFIX}-cache"
    "${PROJECT_PREFIX}-chroma"
    "${PROJECT_PREFIX}-csv-sync"
    "${PROJECT_PREFIX}-media"
    "${PROJECT_PREFIX}-pulse"
    "${PROJECT_PREFIX}-janitor"
    "${PROJECT_PREFIX}-lavalink"
)

RUNNING_CONTAINERS=()
for container in "${CONTAINERS[@]}"; do
    if _container_running "$container"; then
        RUNNING_CONTAINERS+=("$container")
    fi
done

if [[ ${#RUNNING_CONTAINERS[@]} -gt 0 ]]; then
    if [[ "$FORCE" == "true" ]]; then
        warn "Existing containers detected — tearing down (--force):"
        for c in "${RUNNING_CONTAINERS[@]}"; do warn "  • $c"; done
        info "Running: $DC down ..."
        $DC down
        log "Existing stack stopped."
    else
        error "Pre-flight FAILED — the following containers are already running:
$(for c in "${RUNNING_CONTAINERS[@]}"; do echo "  • $c"; done)

  Stop them first with:   $DC down
  Or re-run with:         ./deploy.sh --force"
    fi
else
    log "Container check passed — no conflicts detected."
fi

# =============================================================================
# Step 2: Host Port Availability Validation  (TDR §2-B-2)
# =============================================================================
info "Checking host port availability..."

declare -A PORT_MAP=(
    ["${APP_HOST_PORT}"]="APP_HOST_PORT (orchestrator)"
    ["${MEDIA_PROXY_HOST_PORT}"]="MEDIA_PROXY_HOST_PORT (media-proxy)"
    ["${PULSE_HOST_PORT}"]="PULSE_HOST_PORT (health-sentinel)"
)

PORT_CONFLICTS=()
for port in "${!PORT_MAP[@]}"; do
    if _port_in_use "$port"; then
        PORT_CONFLICTS+=("${port} (${PORT_MAP[$port]})")
    fi
done

if [[ ${#PORT_CONFLICTS[@]} -gt 0 ]]; then
    error "Pre-flight FAILED — the following host ports are already bound:
$(for p in "${PORT_CONFLICTS[@]}"; do echo "  • :${p}"; done)

  Change the affected port variable(s) in .env and retry, or free the port first.
  Tip: set a different value (e.g. APP_HOST_PORT=8080) to run concurrently on another port."
else
    log "Port check passed — all required ports are free."
fi

# =============================================================================
# Step 3: Launch
# =============================================================================
echo ""
info "Pre-flight checks passed — starting stack..."
echo ""
$DC up -d --build "${EXTRA_ARGS[@]}"

echo ""
log "Stack is up!"
echo ""
echo "  Web Admin (White Portal):  http://localhost:${APP_HOST_PORT}/web/"
echo "  Health Pulse:              http://localhost:${PULSE_HOST_PORT}/"
echo "  Media Proxy:               http://localhost:${MEDIA_PROXY_HOST_PORT}/"
echo ""
echo "  View logs:  $DC logs -f"
echo "  Stop:       $DC down"
echo ""

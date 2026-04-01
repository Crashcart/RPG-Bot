#!/usr/bin/env bash
# =============================================================================
# Ironclad GM — One-command uninstaller
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/Crashcart/RPG-Bot/main/uninstall.sh)
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()   { echo -e "${GREEN}[\u2713]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[\u2717]${NC} $*" >&2; exit 1; }
info()  { echo -e "${CYAN}[→]${NC} $*"; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║        Ironclad GM — Uninstaller                     ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

warn "This will permanently remove all Ironclad GM containers, Docker volumes, and local data."
warn "This action is IRREVERSIBLE. All campaign data, character sheets, and logs will be deleted."
echo ""

read -r -p "  Type 'yes' to confirm and continue (anything else cancels): " CONFIRM </dev/tty
if [[ "$CONFIRM" != "yes" ]]; then
    echo "  Cancelled. No changes were made."
    exit 0
fi
echo ""

# =============================================================================
# Detect docker compose
# =============================================================================
if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    warn "Docker Compose not found — skipping container removal."
    DC=""
fi

# =============================================================================
# Step 1: Stop and remove containers + named volumes
# =============================================================================
if [[ -n "$DC" && -f "docker-compose.yml" ]]; then
    info "Stopping containers and removing volumes..."
    $DC down -v --remove-orphans 2>/dev/null || true
    log "Containers and Docker volumes removed"
elif [[ -n "$DC" ]]; then
    warn "docker-compose.yml not found in current directory — skipping docker down."
    warn "If containers are still running, stop them manually: docker ps | grep aetheris"
fi

# =============================================================================
# Step 2: Remove local data directories
# =============================================================================
info "Removing local data directories..."
for dir in data logs backups; do
    if [[ -d "./$dir" ]]; then
        rm -rf "./$dir"
        log "  Removed ./$dir"
    else
        echo "    ./$dir not found — skipping"
    fi
done

# =============================================================================
# Step 3: Optionally remove .env (contains credentials)
# =============================================================================
if [[ -f ".env" ]]; then
    echo ""
    read -r -p "  Remove .env (contains your API keys and passwords)? (y/N): " REMOVE_ENV </dev/tty
    if [[ "$REMOVE_ENV" =~ ^[Yy]$ ]]; then
        rm -f .env .env.backup 2>/dev/null || true
        log ".env removed"
    else
        warn ".env kept. Delete it manually when ready: rm .env"
    fi
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║     ✓  Ironclad GM has been uninstalled.              ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  All containers, Docker volumes, and local data have been removed."
echo ""
echo "  To reinstall:"
echo "    bash <(curl -fsSL https://raw.githubusercontent.com/Crashcart/RPG-Bot/main/install.sh)"
echo ""

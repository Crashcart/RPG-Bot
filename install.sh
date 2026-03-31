#!/usr/bin/env bash
# =============================================================================
# Ironclad GM — One-command installer
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/Crashcart/RPG-Bot/main/install.sh)
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
REPO_URL="https://github.com/Crashcart/RPG-Bot.git"
REPO_DIR="RPG-Bot"

log()   { echo -e "${GREEN}[\u2713]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[\u2717]${NC} $*" >&2; exit 1; }
info()  { echo -e "${CYAN}[→]${NC} $*"; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║          Ironclad GM — Installer                     ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# =============================================================================
# Step 1: Prerequisite checks
# =============================================================================
info "Checking prerequisites..."

command -v docker &>/dev/null || error "Docker is not installed. See: https://docs.docker.com/get-docker/"
command -v git    &>/dev/null || error "git is not installed."
command -v curl   &>/dev/null || error "curl is not installed."

if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    error "Docker Compose not found. See: https://docs.docker.com/compose/install/"
fi

log "Prerequisites satisfied (using: $DC)"

# =============================================================================
# Step 2: Clone repo or detect existing checkout
# =============================================================================
if [[ ! -f "docker-compose.yml" ]]; then
    info "Cloning repository into ./$REPO_DIR ..."
    git clone "$REPO_URL" "$REPO_DIR"
    cd "$REPO_DIR"
    log "Cloned into $(pwd)"
else
    log "Already inside the RPG-Bot repository at $(pwd)"
fi

# =============================================================================
# Step 3: Environment setup
# =============================================================================
if [[ -f ".env" ]]; then
    warn ".env already exists — skipping environment setup. Edit it manually if needed."
else
    cp .env.example .env
    info "Created .env from .env.example"
    echo ""
    echo "  Enter your credentials below. Press Enter to accept an auto-generated value."
    echo ""

    # Helper: generate a secure random hex string
    _rand()   { openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))"; }
    _secret() { openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))"; }

    # Helper: prompt for a value and write it into .env via sed
    # Usage: set_env KEY "Prompt text" secret|visible [default_value]
    set_env() {
        local key="$1" prompt="$2" mode="$3" default="${4:-}"
        local value=""
        if [[ "$mode" == "auto" ]]; then
            sed -i "s|^${key}=.*|${key}=${default}|" .env
            return
        fi
        if [[ "$mode" == "secret" ]]; then
            read -r -s -p "  ${prompt}: " value </dev/tty; echo ""
        else
            read -r    -p "  ${prompt}: " value </dev/tty
        fi
        if [[ -z "$value" && -n "$default" ]]; then
            value="$default"
            echo "    (auto-generated)"
        fi
        if [[ -n "$value" ]]; then
            sed -i "s|^${key}=.*|${key}=${value}|" .env
        fi
    }

    set_env "DISCORD_BOT_TOKEN"      "Discord Bot Token (required)"                    "secret"
    set_env "DISCORD_APPLICATION_ID" "Discord Application ID (required)"               "visible"
    set_env "POSTGRES_PASSWORD"      "PostgreSQL password   [Enter to auto-generate]"  "secret"  "$(_rand)"
    set_env "REDIS_PASSWORD"         "Redis password        [Enter to auto-generate]"  "secret"  "$(_rand)"
    set_env "GEMINI_API_KEY"         "Google Gemini API key [Enter to skip]"            "secret"
    set_env "LAVALINK_PASSWORD"      "Lavalink password     [Enter to auto-generate]"  "secret"  "$(_rand)"
    # SESSION_SECRET_KEY is always auto-generated — no need to prompt
    set_env "SESSION_SECRET_KEY"     "" "auto" "$(_secret)"

    log ".env configured"
fi

# Load env vars for use in migration step (POSTGRES_USER, POSTGRES_DB, OLLAMA_MODEL)
set -a
# shellcheck source=/dev/null
source .env 2>/dev/null || true
set +a

# =============================================================================
# Step 4: Build and start all services
# =============================================================================
info "Building and starting services (this may take a few minutes on first run)..."
$DC up -d --build
log "All services started"

# =============================================================================
# Step 5: Wait for database to be healthy
# =============================================================================
info "Waiting for database to become healthy..."
TIMEOUT=120; ELAPSED=0; INTERVAL=5
until docker inspect --format='{{.State.Health.Status}}' aetheris-db 2>/dev/null | grep -q 'healthy'; do
    if [[ $ELAPSED -ge $TIMEOUT ]]; then
        error "Database did not become healthy within ${TIMEOUT}s. Check: docker logs aetheris-db"
    fi
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
    echo -n "."
done
echo ""
log "Database is healthy"

# =============================================================================
# Step 6: Run database migrations 002–011
# (001 is auto-applied by PostgreSQL initdb.d at first start)
# =============================================================================
MIGRATION_DIR="./db/migrations"
if [[ -d "$MIGRATION_DIR" ]]; then
    info "Applying database migrations..."
    # Use a sorted glob so migrations apply in numeric order
    for migration in $(ls "$MIGRATION_DIR"/*.sql 2>/dev/null | sort); do
        filename=$(basename "$migration")
        echo -n "  Applying ${filename} ... "
        if $DC exec -T ironclad-db psql \
               -U "${POSTGRES_USER:-ironclad}" \
               -d "${POSTGRES_DB:-ironclad}" \
               < "$migration"; then
            echo "OK"
        else
            echo "FAILED"
            error "Migration ${filename} failed. Check: docker logs aetheris-db"
        fi
    done
    log "All migrations applied"
else
    warn "Migration directory not found at $MIGRATION_DIR — skipping."
fi

# =============================================================================
# Step 7: Pull the default Ollama model
# =============================================================================
OLLAMA_MODEL="${OLLAMA_MODEL:-mistral:7b-instruct}"
info "Pulling Ollama model: ${OLLAMA_MODEL} (may take several minutes)..."
if $DC exec brain ollama pull "$OLLAMA_MODEL"; then
    log "Ollama model ready: ${OLLAMA_MODEL}"
else
    warn "Could not pull Ollama model automatically."
    warn "Run manually after install: $DC exec brain ollama pull ${OLLAMA_MODEL}"
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║     ✓  Ironclad GM is running!                       ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Web Admin (White Portal):  http://localhost:8000/web/"
echo "  Health Pulse:              http://localhost:58291/"
echo "  Media Proxy:               http://localhost:8001/"
echo ""
echo "  Next steps:"
echo "    1. Invite your Discord bot to your server"
echo "    2. Open White Portal → Settings → Channel Map and configure your channels"
echo "    3. Register your Ollama node(s) in White Portal → Nodes"
echo ""
echo "  Useful commands:"
echo "    View logs:   $DC logs -f"
echo "    Stop:        $DC down"
echo "    Uninstall:   bash <(curl -fsSL https://raw.githubusercontent.com/Crashcart/RPG-Bot/main/uninstall.sh)"
echo ""

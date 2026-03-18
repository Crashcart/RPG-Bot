-- =============================================================================
-- Ironclad GM – PostgreSQL Schema
-- Uses JSONB columns for system-agnostic flexibility: no migrations needed
-- when adding a new TTRPG system.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Enum: Character Status ────────────────────────────────────────────────────
CREATE TYPE character_status AS ENUM ('ALIVE', 'DEAD', 'RETIRED');

-- ── Enum: Command Type ────────────────────────────────────────────────────────
CREATE TYPE command_type AS ENUM ('action', 'slash_command', 'ooc');

-- =============================================================================
-- Table: campaigns
-- Tracks active campaigns and which rule systems are loaded.
-- =============================================================================
CREATE TABLE IF NOT EXISTS campaigns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    guild_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    system          TEXT NOT NULL,                    -- e.g. "D&D 5e", "Cyberpunk 2020"
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    settings        JSONB NOT NULL DEFAULT '{}',      -- campaign-level rule overrides
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, name)
);

-- =============================================================================
-- Table: characters
-- System-agnostic character sheet. All numeric stats live in JSONB.
-- =============================================================================
CREATE TABLE IF NOT EXISTS characters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    player_id       TEXT NOT NULL,                    -- Discord user snowflake
    name            TEXT NOT NULL,
    system          TEXT NOT NULL,                    -- mirrors campaign system
    status          character_status NOT NULL DEFAULT 'ALIVE',
    stats           JSONB NOT NULL DEFAULT '{}',      -- hp, ac, skills, attributes…
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_characters_campaign   ON characters(campaign_id);
CREATE INDEX IF NOT EXISTS idx_characters_player     ON characters(player_id);
CREATE INDEX IF NOT EXISTS idx_characters_status     ON characters(status);
-- GIN index enables fast JSONB key/value queries (e.g. stats->'hp')
CREATE INDEX IF NOT EXISTS idx_characters_stats_gin  ON characters USING GIN (stats);

-- =============================================================================
-- Table: inventories
-- Per-character item storage. Each row is one logical item stack.
-- =============================================================================
CREATE TABLE IF NOT EXISTS inventories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id    UUID NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    item_data       JSONB NOT NULL DEFAULT '{}',
    -- item_data shape:
    --   { "name": str, "quantity": int, "weight": float,
    --     "mechanical_properties": { ... system-specific ... } }
    acquired_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inventories_character ON inventories(character_id);
CREATE INDEX IF NOT EXISTS idx_inventories_item_gin  ON inventories USING GIN (item_data);

-- =============================================================================
-- Table: rule_registry
-- Tracks which rulebook modules / vectorized PDFs are active per campaign.
-- Supports hot-swapping game mechanics without schema migrations.
-- =============================================================================
CREATE TABLE IF NOT EXISTS rule_registry (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    module_name     TEXT NOT NULL,
    module_type     TEXT NOT NULL CHECK (module_type IN ('pdf', 'json', 'vector')),
    chroma_collection TEXT,                           -- ChromaDB collection name for vector modules
    module_data     JSONB NOT NULL DEFAULT '{}',      -- raw JSON rule overrides (for json type)
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, module_name)
);

CREATE INDEX IF NOT EXISTS idx_rule_registry_campaign ON rule_registry(campaign_id);
CREATE INDEX IF NOT EXISTS idx_rule_registry_active   ON rule_registry(campaign_id, active);

-- =============================================================================
-- Table: action_log
-- Immutable audit log of every player action and its mechanical resolution.
-- Critical for session replay and dispute resolution.
-- =============================================================================
CREATE TABLE IF NOT EXISTS action_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id       UUID NOT NULL,                    -- links to the originating intent payload
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE SET NULL,
    character_id    UUID REFERENCES characters(id) ON DELETE SET NULL,
    player_id       TEXT NOT NULL,
    raw_input       TEXT NOT NULL,
    intent_payload  JSONB NOT NULL,                   -- full IntentPayload snapshot
    mechanical_payload JSONB,                         -- OllamaResolutionPayload snapshot
    state_delta     JSONB,                            -- StateDeltaPayload snapshot
    narrative_summary TEXT,                           -- first 500 chars of Gemini output
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_action_log_campaign   ON action_log(campaign_id);
CREATE INDEX IF NOT EXISTS idx_action_log_character  ON action_log(character_id);
CREATE INDEX IF NOT EXISTS idx_action_log_player     ON action_log(player_id);
CREATE INDEX IF NOT EXISTS idx_action_log_intent     ON action_log(intent_id);

-- =============================================================================
-- Table: sessions
-- Active WebSocket / Discord interaction sessions (mirrors Redis TTL).
-- =============================================================================
CREATE TABLE IF NOT EXISTS sessions (
    session_token   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id       TEXT NOT NULL,
    guild_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    campaign_id     UUID REFERENCES campaigns(id) ON DELETE CASCADE,
    character_id    UUID REFERENCES characters(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '1 hour'
);

CREATE INDEX IF NOT EXISTS idx_sessions_player  ON sessions(player_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- =============================================================================
-- Trigger: auto-update updated_at timestamps
-- =============================================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_campaigns_updated_at
    BEFORE UPDATE ON campaigns
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_characters_updated_at
    BEFORE UPDATE ON characters
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_inventories_updated_at
    BEFORE UPDATE ON inventories
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

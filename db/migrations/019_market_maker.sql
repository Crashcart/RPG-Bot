-- Migration 019: Async Market Maker & Deep Supply-Chain Simulation
-- Run: psql -U ironclad -d ironclad -f db/migrations/019_market_maker.sql
-- Issue: https://github.com/Crashcart/RPG-Bot/issues/24

-- ── Node type enum ────────────────────────────────────────────────────────────
CREATE TYPE market_node_type AS ENUM (
    'settlement',
    'space_station',
    'mining_colony',
    'trade_hub',
    'black_market',
    'military_outpost',
    'agricultural_zone',
    'industrial_complex'
);

-- ── market_nodes: locations with production/consumption rates ─────────────────
CREATE TABLE market_nodes (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    node_type       market_node_type NOT NULL DEFAULT 'settlement',
    location_label  TEXT,
    security_rating SMALLINT    NOT NULL DEFAULT 5 CHECK (security_rating BETWEEN 1 AND 10),
    is_enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
    metadata        JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_market_nodes_campaign ON market_nodes (campaign_id);
CREATE INDEX idx_market_nodes_enabled  ON market_nodes (campaign_id, is_enabled);

COMMENT ON TABLE  market_nodes IS 'Trade locations (cities, stations, outposts) that participate in the dynamic economy.';
COMMENT ON COLUMN market_nodes.security_rating IS '1=lawless, 10=high-security; used for contraband interdiction roll threshold.';

-- ── commodities: tradeable goods catalog per campaign ─────────────────────────
CREATE TABLE commodities (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    base_price  NUMERIC(12,2) NOT NULL DEFAULT 10.00 CHECK (base_price > 0),
    is_legal    BOOLEAN     NOT NULL DEFAULT TRUE,
    category    TEXT        NOT NULL DEFAULT 'general',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, name)
);

CREATE INDEX idx_commodities_campaign ON commodities (campaign_id);

COMMENT ON TABLE  commodities IS 'Per-campaign catalog of tradeable goods. base_price is the reference price at 50% supply.';
COMMENT ON COLUMN commodities.is_legal IS 'FALSE = contraband; triggers security check during Phase 2 adjudication.';

-- ── market_inventory: current supply at each node per commodity ───────────────
CREATE TABLE market_inventory (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id         UUID          NOT NULL REFERENCES market_nodes(id)  ON DELETE CASCADE,
    commodity_id    UUID          NOT NULL REFERENCES commodities(id)    ON DELETE CASCADE,
    supply          NUMERIC(12,2) NOT NULL DEFAULT 0    CHECK (supply >= 0),
    production_rate NUMERIC(12,2) NOT NULL DEFAULT 0    CHECK (production_rate >= 0),
    demand_rate     NUMERIC(12,2) NOT NULL DEFAULT 0    CHECK (demand_rate >= 0),
    max_supply      NUMERIC(12,2) NOT NULL DEFAULT 1000 CHECK (max_supply > 0),
    current_price   NUMERIC(12,2) NOT NULL DEFAULT 10.00,
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (node_id, commodity_id)
);

CREATE INDEX idx_market_inv_node      ON market_inventory (node_id);
CREATE INDEX idx_market_inv_commodity ON market_inventory (commodity_id);

COMMENT ON TABLE  market_inventory IS 'Live supply/price state per commodity per node. Updated each economy tick.';
COMMENT ON COLUMN market_inventory.production_rate IS 'Units produced per tick (added to supply, capped at max_supply).';
COMMENT ON COLUMN market_inventory.demand_rate     IS 'Units consumed per tick (subtracted from supply, floor 0).';
COMMENT ON COLUMN market_inventory.current_price   IS 'Recalculated each tick: base_price * (max_supply/2) / max(supply, max_supply*0.05).';

-- ── market_transactions: audit log of all buy/sell/haul events ────────────────
CREATE TABLE market_transactions (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID          NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    node_id         UUID          NOT NULL REFERENCES market_nodes(id),
    commodity_id    UUID          NOT NULL REFERENCES commodities(id),
    character_id    UUID                   REFERENCES characters(id),
    action          TEXT          NOT NULL CHECK (action IN ('buy', 'sell', 'ghost_haul')),
    quantity        NUMERIC(12,2) NOT NULL CHECK (quantity > 0),
    unit_price      NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    total_value     NUMERIC(12,2) NOT NULL,
    is_contraband   BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_market_tx_campaign ON market_transactions (campaign_id, created_at DESC);
CREATE INDEX idx_market_tx_char     ON market_transactions (character_id) WHERE character_id IS NOT NULL;

COMMENT ON TABLE  market_transactions IS 'Immutable audit log of all economy events including ghost freighter equalization hauls.';
COMMENT ON COLUMN market_transactions.action IS 'buy=player purchase, sell=player sale, ghost_haul=NPC equalization run.';

-- ── economy_tick_log: records each tick cycle for debugging ───────────────────
CREATE TABLE economy_tick_log (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id  UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    ticked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    nodes_ticked INTEGER     NOT NULL DEFAULT 0,
    ghost_hauls  INTEGER     NOT NULL DEFAULT 0,
    duration_ms  INTEGER
);

CREATE INDEX idx_economy_tick_campaign ON economy_tick_log (campaign_id, ticked_at DESC);

COMMENT ON TABLE economy_tick_log IS 'Debug log: one row per economy tick execution. Useful for tuning tick frequency.';

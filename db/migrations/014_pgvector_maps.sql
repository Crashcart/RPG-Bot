-- Migration 014: pgvector extension + map/spatial state tables
-- Run: psql -U ironclad -d ironclad -f db/migrations/014_pgvector_maps.sql
--
-- Enables pgvector for spatial similarity search and adds tables for
-- persistent map state, player coordinates, and Fog-of-War tracking.
-- The in-memory hot path (bitmasks + live positions) lives in Redis;
-- this schema provides durable audit trails and async persistence.

-- ── pgvector extension ────────────────────────────────────────────────────────
-- Requires pgvector/pgvector:pg16 image (already specified in docker-compose).
-- Enables VECTOR type and <-> (L2), <#> (inner product), <=> (cosine) operators.
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Table: map_state ──────────────────────────────────────────────────────────
-- Stores the persistent grid layout and Fog-of-War snapshot for each campaign.
-- Live FoW bitmasks are managed by Redis; this table is the durable checkpoint.
CREATE TABLE IF NOT EXISTS map_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    map_name        TEXT NOT NULL DEFAULT 'default',
    -- Grid dimensions
    cols            INTEGER NOT NULL DEFAULT 20,
    rows            INTEGER NOT NULL DEFAULT 20,
    tile_size_px    INTEGER NOT NULL DEFAULT 32,
    -- Fog-of-War snapshot: flat array of revealed cell indices stored as JSONB
    revealed_cells  JSONB   NOT NULL DEFAULT '[]',
    -- Optional background image asset path served by media-proxy
    background_asset TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, map_name)
);

CREATE INDEX IF NOT EXISTS idx_map_state_campaign ON map_state(campaign_id);

-- ── Table: map_entities ───────────────────────────────────────────────────────
-- Durable record of every entity (player, NPC, object) on the map grid.
-- Coordinate columns are also stored as a pgvector(2) for spatial similarity
-- queries (e.g. "find all entities within N tiles of player X").
CREATE TABLE IF NOT EXISTS map_entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    map_id          UUID NOT NULL REFERENCES map_state(id) ON DELETE CASCADE,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('player', 'npc', 'object')),
    entity_ref      TEXT NOT NULL,   -- Discord user ID or NPC/object identifier
    token_label     TEXT NOT NULL DEFAULT '',
    -- Integer grid coordinates (canonical)
    grid_x          INTEGER NOT NULL DEFAULT 0,
    grid_y          INTEGER NOT NULL DEFAULT 0,
    -- pgvector(2) spatial column for distance-based lookups
    position        vector(2),
    metadata        JSONB   NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_map_entities_map     ON map_entities(map_id);
CREATE INDEX IF NOT EXISTS idx_map_entities_ref     ON map_entities(entity_ref);
-- IVFFlat index for approximate nearest-neighbour position queries
CREATE INDEX IF NOT EXISTS idx_map_entities_pos_ivf
    ON map_entities USING ivfflat (position vector_l2_ops)
    WITH (lists = 10);

-- ── Auto-update trigger for map_state ────────────────────────────────────────
CREATE TRIGGER trg_map_state_updated_at
    BEFORE UPDATE ON map_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION set_map_entity_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    -- Keep pgvector position in sync with integer grid columns
    NEW.position = ARRAY[NEW.grid_x::float, NEW.grid_y::float]::vector(2);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_map_entities_updated_at
    BEFORE INSERT OR UPDATE ON map_entities
    FOR EACH ROW EXECUTE FUNCTION set_map_entity_updated_at();

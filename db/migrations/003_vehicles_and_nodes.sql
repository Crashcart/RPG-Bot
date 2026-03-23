-- =============================================================================
-- Migration 003 – Vehicle / Asset Subsystems + AI Node Registry
-- =============================================================================

-- ── Shared enum: operational status for vehicle subsystems ───────────────────
CREATE TYPE IF NOT EXISTS operational_status AS ENUM (
    'OPERATIONAL',
    'DAMAGED',
    'DESTROYED'
);

-- =============================================================================
-- Table: vehicles
-- Campaign-level physical assets (ships, mechs, vehicles, installations).
-- Hull integrity and high-level asset stats live here; per-component detail
-- lives in vehicle_subsystems.
-- =============================================================================
CREATE TABLE IF NOT EXISTS vehicles (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id         UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    asset_type          TEXT NOT NULL DEFAULT 'ship',   -- ship, mech, vehicle, station…
    hull_integrity      INTEGER NOT NULL DEFAULT 100,
    max_hull_integrity  INTEGER NOT NULL DEFAULT 100,
    -- asset_data: speed, maneuverability, crew_capacity, sensor_range, shields…
    asset_data          JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vehicles_campaign ON vehicles(campaign_id);
CREATE INDEX IF NOT EXISTS idx_vehicles_data_gin ON vehicles USING GIN (asset_data);

CREATE TRIGGER trg_vehicles_updated_at
    BEFORE UPDATE ON vehicles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- Table: vehicle_subsystems
-- Individual components of a vehicle.  A player claims a seat by being
-- assigned to a subsystem.  Ollama can change operational_status and
-- assigned_character_id as part of a vehicle_delta.
--
-- subsystem_data shape (example — weapon):
--   { "damage_dice": "2d6", "damage_type": "ballistic", "range_m": 500,
--     "targeting_bonus": 2, "ammo": 40, "ammo_max": 40, "burst_capable": true }
--
-- subsystem_data shape (example — propulsion):
--   { "thrust": 4, "maneuver_rating": 2 }
-- =============================================================================
CREATE TABLE IF NOT EXISTS vehicle_subsystems (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id            UUID NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    subsystem_name        TEXT NOT NULL,
    subsystem_type        TEXT NOT NULL CHECK (subsystem_type IN (
                              'weapon', 'defense', 'propulsion', 'sensor', 'utility', 'medical'
                          )),
    operational_status    operational_status NOT NULL DEFAULT 'OPERATIONAL',
    assigned_character_id UUID REFERENCES characters(id) ON DELETE SET NULL,
    subsystem_data        JSONB NOT NULL DEFAULT '{}',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (vehicle_id, subsystem_name)
);

CREATE INDEX IF NOT EXISTS idx_vehicle_subsystems_vehicle   ON vehicle_subsystems(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_subsystems_character ON vehicle_subsystems(assigned_character_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_subsystems_data_gin  ON vehicle_subsystems USING GIN (subsystem_data);

CREATE TRIGGER trg_vehicle_subsystems_updated_at
    BEFORE UPDATE ON vehicle_subsystems
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- Table: node_registry
-- Tracks every AI processing backend available to the orchestrator.
-- The NodeRouter service reads this table to pick the best available node for
-- each adjudication request, enabling the "Hybrid AI Mesh" architecture.
--
-- For Ollama nodes: host = "http://192.168.1.50:11434", model = "mistral:7b"
-- For the Gemini cloud node: host = "https://generativelanguage.googleapis.com",
--   model = "gemini-1.5-pro" (api_key is read from environment, NOT stored here)
-- =============================================================================
CREATE TYPE IF NOT EXISTS node_type   AS ENUM ('ollama', 'gemini');
CREATE TYPE IF NOT EXISTS node_status AS ENUM ('online', 'offline', 'degraded', 'unknown');

CREATE TABLE IF NOT EXISTS node_registry (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_name   TEXT NOT NULL UNIQUE,
    node_type   node_type   NOT NULL DEFAULT 'ollama',
    host        TEXT NOT NULL,             -- base URL
    model       TEXT NOT NULL DEFAULT '',  -- model identifier
    priority    INTEGER NOT NULL DEFAULT 10,  -- lower = higher preference
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    status      node_status NOT NULL DEFAULT 'unknown',
    last_seen   TIMESTAMPTZ,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_node_registry_type_enabled ON node_registry(node_type, enabled);

CREATE TRIGGER trg_node_registry_updated_at
    BEFORE UPDATE ON node_registry
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

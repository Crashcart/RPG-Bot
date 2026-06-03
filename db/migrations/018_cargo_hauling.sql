-- =============================================================================
-- Migration 018 – Asynchronous Cargo Hauling & Spatial Routing
-- Issue #21 — background spatial worker for vehicle transit
-- =============================================================================

-- ── Transit state enum ────────────────────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE transit_state AS ENUM (
        'idle',
        'in_transit',
        'interdicted',
        'docked'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── Transit event type enum ───────────────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE transit_event_type AS ENUM (
        'departure',
        'arrival',
        'interdiction',
        'fuel_warning',
        'fuel_empty',
        'course_update'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── Nav computer state column on vehicles ────────────────────────────────────
-- nav_computer JSONB shape:
--   {
--     "transit_state": "in_transit",
--     "origin_name": "Kepler Station",
--     "destination_name": "Mining Colony Theta",
--     "origin_x": 0.0, "origin_y": 0.0, "origin_z": 0.0,
--     "dest_x": 450.0, "dest_y": 120.0, "dest_z": -30.0,
--     "current_x": 90.0, "current_y": 24.0, "current_z": -6.0,
--     "speed": 10.0,            -- units per tick
--     "distance_total": 471.4,
--     "distance_remaining": 377.1,
--     "eta_seconds": 37710,
--     "fuel_remaining": 80.0,
--     "fuel_capacity": 100.0,
--     "fuel_per_tick": 0.5,
--     "departure_at": "2026-06-03T12:00:00Z",
--     "interdiction_hazard": null    -- set when state=interdicted
--   }
ALTER TABLE vehicles
    ADD COLUMN IF NOT EXISTS transit_state transit_state NOT NULL DEFAULT 'idle',
    ADD COLUMN IF NOT EXISTS nav_computer  JSONB         NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_vehicles_transit_state
    ON vehicles(transit_state)
    WHERE transit_state = 'in_transit';

CREATE INDEX IF NOT EXISTS idx_vehicles_nav_gin
    ON vehicles USING GIN (nav_computer);

-- ── Hazard zones ──────────────────────────────────────────────────────────────
-- Simple spherical zones.  The spatial worker checks Euclidean distance from a
-- vehicle's current coordinates to the zone centre on every tick.
CREATE TABLE IF NOT EXISTS hazard_zones (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID    NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    -- zone_type: asteroid_field | pirate_patrol | ion_storm | military_blockade | custom
    zone_type   TEXT    NOT NULL DEFAULT 'custom',
    center_x    FLOAT   NOT NULL DEFAULT 0.0,
    center_y    FLOAT   NOT NULL DEFAULT 0.0,
    center_z    FLOAT   NOT NULL DEFAULT 0.0,
    radius      FLOAT   NOT NULL DEFAULT 50.0,
    -- Optional structured metadata (encounter tables, threat ratings, etc.)
    zone_data   JSONB   NOT NULL DEFAULT '{}',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hazard_zones_campaign
    ON hazard_zones(campaign_id, enabled);

CREATE TRIGGER trg_hazard_zones_updated_at
    BEFORE UPDATE ON hazard_zones
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Transit event log ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transit_log (
    id              UUID               PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      UUID               NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    campaign_id     UUID               NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    event_type      transit_event_type NOT NULL,
    -- Snapshot of coordinates at the time of the event
    x               FLOAT    NOT NULL DEFAULT 0.0,
    y               FLOAT    NOT NULL DEFAULT 0.0,
    z               FLOAT    NOT NULL DEFAULT 0.0,
    sector_name     TEXT     NOT NULL DEFAULT '',
    description     TEXT     NOT NULL DEFAULT '',
    -- Arbitrary event payload (hazard data, interdiction details, etc.)
    event_data      JSONB    NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transit_log_vehicle
    ON transit_log(vehicle_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_transit_log_campaign
    ON transit_log(campaign_id, created_at DESC);

-- =============================================================================
-- Migration 014 — World Object Tracker
-- Persistent visual and textual state for in-game objects across sessions.
-- Issue #7: Persistent Visual & Textual Object State Tracker
-- =============================================================================

-- Status lifecycle:
--   active    → default; mutations allowed
--   locked    → contents frozen (puzzle box, sealed vault); mutations blocked
--   consumed  → terminal; item used up (potion drunk, scroll cast)
--   destroyed → terminal; item broken/gone
DO $$ BEGIN
    CREATE TYPE world_object_status AS ENUM (
        'active',
        'locked',
        'consumed',
        'destroyed'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- =============================================================================
-- Table: world_objects
-- One row per logical in-game entity (weapon, container, location prop, etc.)
-- =============================================================================
CREATE TABLE IF NOT EXISTS world_objects (
    entity_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id         UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,

    -- Immutable identity (written once at registration, never overwritten)
    base_description    TEXT NOT NULL,
    image_url           TEXT NOT NULL DEFAULT '',

    -- Mutable game state — free-form JSONB
    -- Examples: {"charges": 3}, {"contents": ["uuid-a", "uuid-b"]}, {"broken": true}
    current_state       JSONB NOT NULL DEFAULT '{}',

    -- Parent-child nesting: a backpack "owns" its contents via this FK
    parent_entity_id    UUID REFERENCES world_objects(entity_id) ON DELETE SET NULL,

    object_status       world_object_status NOT NULL DEFAULT 'active',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Campaign-scoped listing
CREATE INDEX IF NOT EXISTS idx_world_objects_campaign
    ON world_objects(campaign_id);

-- Children lookup (sparse — only rows with a parent)
CREATE INDEX IF NOT EXISTS idx_world_objects_parent
    ON world_objects(parent_entity_id)
    WHERE parent_entity_id IS NOT NULL;

-- Status filter
CREATE INDEX IF NOT EXISTS idx_world_objects_status
    ON world_objects(campaign_id, object_status);

-- JSONB key/value queries (e.g. current_state->'charges')
CREATE INDEX IF NOT EXISTS idx_world_objects_state_gin
    ON world_objects USING GIN (current_state);

CREATE TRIGGER trg_world_objects_updated_at
    BEFORE UPDATE ON world_objects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- Migration 014 – Persistent Visual & Textual Object State Tracker
--
-- Implements the dual-storage object registry described in TDR: Persistent
-- Visual & Textual Object State Tracker.  All in-game objects (items,
-- containers, locations, artefacts…) are registered here with a UUID so
-- the engine can retrieve their exact image, immutable description, and
-- current dynamic state across sessions.
--
-- Key design decisions:
--   • base_description is written once at registration and never updated –
--     the trigger enforces this.
--   • current_state is an ENUM with validation: locked/destroyed entities
--     reject contents mutations at the application layer.
--   • inventory_array (JSONB array) holds child entity UUIDs or inline
--     item descriptors.  Full parent-child relationships use owner_entity_id
--     so the DB can enforce referential integrity.
--   • phash stores a 64-bit perceptual hash of the associated image to
--     allow deduplication at image-registration time.
-- =============================================================================

-- ── State enum ────────────────────────────────────────────────────────────────
CREATE TYPE entity_object_state AS ENUM ('active', 'locked', 'destroyed');

-- ── entity_objects ────────────────────────────────────────────────────────────
CREATE TABLE entity_objects (
    entity_id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id       UUID         NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,

    -- Taxonomy
    entity_type       TEXT         NOT NULL DEFAULT 'item',
    -- e.g. item | container | location | npc | vehicle | artefact

    -- Visual asset
    image_url         TEXT         NOT NULL DEFAULT '',
    -- Media-proxy URL (/assets/gen/…) or empty if no image yet

    phash             BIGINT,
    -- 64-bit perceptual hash of the image for deduplication (NULL = no image)

    -- Immutable after creation (enforced by trigger below)
    base_description  TEXT         NOT NULL DEFAULT '',
    -- One paragraph of canonical description.  The engine injects this verbatim.

    -- Mutable state
    current_state     entity_object_state NOT NULL DEFAULT 'active',

    -- Contents / inventory (parent-child)
    inventory_array   JSONB        NOT NULL DEFAULT '[]',
    -- Array of child entity UUIDs or inline item descriptors:
    --   ["<uuid>", {"name": "Gold Coin", "qty": 3}, …]

    -- Parent relationship
    owner_entity_id   UUID         REFERENCES entity_objects(entity_id) ON DELETE SET NULL,
    -- NULL = top-level object; non-null = owned by another entity

    -- Metadata
    display_name      TEXT         NOT NULL DEFAULT '',
    extra_data        JSONB        NOT NULL DEFAULT '{}',
    -- Free-form system-specific metadata (stats, magic properties, etc.)

    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entity_objects_campaign  ON entity_objects(campaign_id);
CREATE INDEX idx_entity_objects_owner     ON entity_objects(owner_entity_id);
CREATE INDEX idx_entity_objects_phash     ON entity_objects(phash) WHERE phash IS NOT NULL;
CREATE INDEX idx_entity_objects_state     ON entity_objects(current_state);
CREATE INDEX idx_entity_objects_inv_gin   ON entity_objects USING GIN (inventory_array);

-- ── updated_at trigger ────────────────────────────────────────────────────────
CREATE TRIGGER trg_entity_objects_updated_at
    BEFORE UPDATE ON entity_objects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Immutable base_description guard ─────────────────────────────────────────
-- Raise an exception if anything tries to change base_description after insert.
CREATE OR REPLACE FUNCTION guard_base_description()
    RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.base_description IS DISTINCT FROM OLD.base_description THEN
        RAISE EXCEPTION
            'base_description is immutable for entity %. Set it once at registration.',
            OLD.entity_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_entity_objects_base_desc_immutable
    BEFORE UPDATE ON entity_objects
    FOR EACH ROW
    WHEN (NEW.base_description IS DISTINCT FROM OLD.base_description)
    EXECUTE FUNCTION guard_base_description();

-- ── entity_object_history ─────────────────────────────────────────────────────
-- Append-only audit log for every state mutation.  Enables full replay of an
-- object's lifecycle (what was inside the chest three sessions ago?).
CREATE TABLE entity_object_history (
    history_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID         NOT NULL REFERENCES entity_objects(entity_id) ON DELETE CASCADE,
    campaign_id     UUID         NOT NULL,
    changed_by      TEXT         NOT NULL DEFAULT 'system',
    -- Discord snowflake, player UUID, or 'system'/'gm'

    previous_state  entity_object_state NOT NULL,
    new_state       entity_object_state NOT NULL,
    previous_inv    JSONB        NOT NULL DEFAULT '[]',
    new_inv         JSONB        NOT NULL DEFAULT '[]',
    change_note     TEXT         NOT NULL DEFAULT '',
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entity_history_entity     ON entity_object_history(entity_id);
CREATE INDEX idx_entity_history_campaign   ON entity_object_history(campaign_id);
CREATE INDEX idx_entity_history_changed_at ON entity_object_history(changed_at);

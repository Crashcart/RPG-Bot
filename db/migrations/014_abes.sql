-- =============================================================================
-- Migration 014 – Autonomous Background Entity Simulation (ABES)
--   • npc_entities  – NPC/faction entities with active long-term intents
--   • world_delta   – lightweight event log for background world changes
-- =============================================================================

-- =============================================================================
-- Table: npc_entities
-- Tracks autonomous NPCs, factions, creatures, and vehicles that the ABES
-- engine advances every world tick without invoking the AI narration pipeline.
-- =============================================================================
CREATE TABLE IF NOT EXISTS npc_entities (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id          UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name                 TEXT        NOT NULL,
    entity_type          TEXT        NOT NULL DEFAULT 'npc'
                             CHECK (entity_type IN ('npc', 'faction', 'creature', 'vehicle')),
    current_location     TEXT        NOT NULL DEFAULT '',   -- free-text or coordinate string
    destination          TEXT        NOT NULL DEFAULT '',   -- target location when travelling
    intent_type          TEXT        NOT NULL DEFAULT 'idle'
                             CHECK (intent_type IN (
                                 'idle', 'travel', 'trade', 'craft', 'forage',
                                 'patrol', 'siege', 'recruit', 'rest', 'custom'
                             )),
    intent_description   TEXT        NOT NULL DEFAULT '',   -- human-readable intent
    stats                JSONB       NOT NULL DEFAULT '{}', -- hp, max_hp, speed, morale …
    active               BOOLEAN     NOT NULL DEFAULT TRUE,
    tick_interval_hours  INTEGER     NOT NULL DEFAULT 1
                             CHECK (tick_interval_hours BETWEEN 1 AND 168),
    last_ticked_at       TIMESTAMPTZ,
    next_tick_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_npc_entities_campaign ON npc_entities(campaign_id);
CREATE INDEX IF NOT EXISTS idx_npc_entities_active
    ON npc_entities(next_tick_at) WHERE active = TRUE;

CREATE TRIGGER trg_npc_entities_updated_at
    BEFORE UPDATE ON npc_entities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- Table: world_delta
-- Lightweight append-only log of significant background world events.
-- The RAG catch-up phase reads this table to rehydrate cold state into
-- organic in-character rumours when a player logs back in.
--
-- significance:
--   minor    – routine progress (NPC moves one step, resources gathered)
--   major    – objective completed, faction reaches destination
--   critical – NPC death, location changes ownership → sets flagged = TRUE
--              so the tick engine pauses that entity's simulation until a
--              player resolves the crisis manually (Option 3 interrupt).
-- =============================================================================
CREATE TABLE IF NOT EXISTS world_delta (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id      UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    entity_id        UUID        REFERENCES npc_entities(id) ON DELETE SET NULL,
    event_type       TEXT        NOT NULL,   -- e.g. 'entity_moved', 'entity_died'
    summary          TEXT        NOT NULL,   -- human-readable one-liner
    mechanical_data  JSONB       NOT NULL DEFAULT '{}',
    significance     TEXT        NOT NULL DEFAULT 'minor'
                         CHECK (significance IN ('minor', 'major', 'critical')),
    flagged          BOOLEAN     NOT NULL DEFAULT FALSE,  -- TRUE = player must resolve before sim resumes
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified         BOOLEAN     NOT NULL DEFAULT FALSE   -- TRUE once Discord webhook has fired
);

CREATE INDEX IF NOT EXISTS idx_world_delta_campaign    ON world_delta(campaign_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_world_delta_entity      ON world_delta(entity_id);
CREATE INDEX IF NOT EXISTS idx_world_delta_unnotified
    ON world_delta(occurred_at) WHERE notified = FALSE AND significance = 'critical';

-- =============================================================================
-- Seed ABES system-settings defaults
-- =============================================================================
INSERT INTO global_settings (key, value) VALUES
    ('abes_enabled',               'true'),
    ('abes_tick_interval_hours',   '1'),
    ('abes_time_dilation_factor',  '1.0'),
    ('abes_webhook_url',           '""')
ON CONFLICT (key) DO NOTHING;

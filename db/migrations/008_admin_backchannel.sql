-- =============================================================================
-- Migration 008 – Admin Backchannel & Fair Play Protocol
--   • gm_directives   – OOC admin commands injected into upcoming narratives
--   • fair_play_mode  – system_settings seed
-- =============================================================================

-- =============================================================================
-- Table: gm_directives
-- Stores out-of-character (OOC) world-management commands submitted by the
-- Admin through the White Portal's private Backchannel interface.
--
-- The GM Director consumes pending directives on the next player action in
-- the same campaign and weaves them into the narrative as high-priority world
-- events.  Each directive is consumed exactly once (one-shot) and archived
-- here for the audit trail.
--
-- directive_type options:
--   scene_directive  – "trigger a massive storm in the next scene"
--   npc_hint         – "have the bartender drop a hint about the catacombs"
--   world_event      – "three soldiers march through the square right now"
--   pacing_note      – "this is a pivotal moment, play it for maximum tension"
--   correction       – "the players missed the clue, help subtly without railroading"
-- =============================================================================
CREATE TYPE IF NOT EXISTS directive_status AS ENUM ('pending', 'consumed', 'cancelled');
CREATE TYPE IF NOT EXISTS directive_type   AS ENUM (
    'scene_directive', 'npc_hint', 'world_event', 'pacing_note', 'correction'
);

CREATE TABLE IF NOT EXISTS gm_directives (
    id               UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id      UUID             NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    admin_id         TEXT             NOT NULL,   -- Discord snowflake of the issuing admin
    directive_type   directive_type   NOT NULL DEFAULT 'scene_directive',
    directive_text   TEXT             NOT NULL,   -- Verbatim admin instruction
    priority         INTEGER          NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    -- priority 10 = inject regardless of scene context; 1 = only if relevant
    status           directive_status NOT NULL DEFAULT 'pending',
    submitted_at     TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    consumed_at      TIMESTAMPTZ,
    consumed_intent_id UUID                       -- which action_log intent consumed this
);

CREATE INDEX IF NOT EXISTS idx_gm_directives_campaign ON gm_directives(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_gm_directives_pending
    ON gm_directives(campaign_id, priority DESC, submitted_at ASC)
    WHERE status = 'pending';

-- =============================================================================
-- Seed system_settings for Fair Play mode and Admin backchannel
-- =============================================================================
INSERT INTO system_settings (key, value)
VALUES
    -- When true, Admin Discord accounts are treated as standard players.
    -- Their White Portal privileges do not extend into Discord channels.
    ('fair_play_mode', 'true'),
    -- Maximum number of pending directives injected per player turn
    ('max_directives_per_turn', '3')
ON CONFLICT (key) DO NOTHING;

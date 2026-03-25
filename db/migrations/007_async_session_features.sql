-- =============================================================================
-- Migration 007 – Async Session Features
--   • downtime_tasks  – background tasks players submit before logging off
--   • player_presence – tracks online/offline state for Campfire Mode
--   • retcon_log      – audit trail for admin rollbacks
-- =============================================================================

-- =============================================================================
-- Table: downtime_tasks
-- Players declare what their character is doing offline (researching, crafting,
-- training, etc.).  The orchestrator resolves the task via Ollama/Gemini in the
-- background and delivers the result as a DM when the player returns.
--
-- status flow:  pending → resolving → complete | failed
-- =============================================================================
CREATE TABLE IF NOT EXISTS downtime_tasks (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id       UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    player_id         TEXT        NOT NULL,   -- Discord snowflake
    character_id      UUID        REFERENCES characters(id) ON DELETE CASCADE,
    description       TEXT        NOT NULL,   -- Verbatim player request
    duration_hours    INTEGER     NOT NULL DEFAULT 8 CHECK (duration_hours BETWEEN 1 AND 168),
    status            TEXT        NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'resolving', 'complete', 'failed')),
    result_narrative  TEXT,                   -- GM-generated DM text delivered to player
    mechanical_result JSONB,                  -- stat / inventory changes (may be empty)
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolves_at       TIMESTAMPTZ NOT NULL,   -- submitted_at + duration_hours
    resolved_at       TIMESTAMPTZ,
    notified          BOOLEAN     NOT NULL DEFAULT FALSE  -- TRUE once DM has been sent
);

CREATE INDEX IF NOT EXISTS idx_downtime_player    ON downtime_tasks(player_id);
CREATE INDEX IF NOT EXISTS idx_downtime_campaign  ON downtime_tasks(campaign_id);
-- Partial index: only rows the resolver loop needs to touch
CREATE INDEX IF NOT EXISTS idx_downtime_pending   ON downtime_tasks(resolves_at)
    WHERE status = 'pending';

-- =============================================================================
-- Table: player_presence
-- One row per (player, guild).  Written by the Discord bot's on_presence_update
-- handler and read by the orchestrator to decide whether to engage Campfire Mode.
-- =============================================================================
CREATE TABLE IF NOT EXISTS player_presence (
    player_id    TEXT        NOT NULL,
    guild_id     TEXT        NOT NULL,
    online       BOOLEAN     NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (player_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_presence_guild ON player_presence(guild_id, online);

-- =============================================================================
-- Table: retcon_log
-- Audit trail every time an admin rolls back an action.
-- The original action_log row is NOT deleted — it is flagged retconned = TRUE
-- so the full history is preserved for dispute resolution.
-- =============================================================================

-- First add a retconned flag to action_log
ALTER TABLE action_log
    ADD COLUMN IF NOT EXISTS retconned     BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS retconned_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retconned_by  TEXT;           -- admin Discord snowflake

CREATE INDEX IF NOT EXISTS idx_action_log_retconned
    ON action_log(retconned) WHERE retconned = TRUE;

CREATE TABLE IF NOT EXISTS retcon_log (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id           UUID        NOT NULL,   -- references action_log.intent_id
    retconned_by        TEXT        NOT NULL,   -- admin player_id
    pre_state_snapshot  JSONB       NOT NULL,   -- character stats before the retconned action
    post_state_snapshot JSONB       NOT NULL,   -- stats that were applied (now reversed)
    reason              TEXT        NOT NULL DEFAULT '',
    retconned_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_retcon_log_intent ON retcon_log(intent_id);

-- =============================================================================
-- Seed system_settings defaults for Campfire Mode
-- =============================================================================
INSERT INTO system_settings (key, value)
VALUES
    ('campfire_mode_active',    'false'),
    ('campfire_absent_players', '[]')
ON CONFLICT (key) DO NOTHING;

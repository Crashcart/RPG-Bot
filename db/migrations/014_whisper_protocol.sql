-- Migration 014: Whisper Protocol — hidden psychological state + perception triggers
-- Run: psql -U ironclad -d ironclad -f db/migrations/014_whisper_protocol.sql

-- ── Hidden State on Characters ───────────────────────────────────────────────
-- Stores sanity, fear, and status flags that are NOT shown to the player.
-- Phase 3 mutates this column; Phase 4 reads it to decide whisper content.
ALTER TABLE characters
    ADD COLUMN IF NOT EXISTS hidden_state JSONB NOT NULL DEFAULT '{}';

-- GIN index: fast containment queries like hidden_state @> '{"paranoid": true}'
CREATE INDEX IF NOT EXISTS idx_characters_hidden_state
    ON characters USING GIN (hidden_state);

-- ── Whisper Log ───────────────────────────────────────────────────────────────
-- Immutable audit trail of every whisper delivered to a player.
-- Rows are never deleted; kept for session replay and GM review.
CREATE TABLE IF NOT EXISTS whisper_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    character_id    UUID        NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    campaign_id     UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    intent_id       UUID        NOT NULL,           -- action_log FK (soft reference)
    trigger_reason  TEXT        NOT NULL,           -- e.g. "sanity_check", "eldritch_gaze"
    sanity_delta    INTEGER     NOT NULL DEFAULT 0, -- negative = drain
    flags_applied   JSONB       NOT NULL DEFAULT '{}',
    whisper_text    TEXT,                           -- the actual whisper delivered
    delivered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whisper_log_character
    ON whisper_log (character_id, delivered_at DESC);

CREATE INDEX IF NOT EXISTS idx_whisper_log_campaign
    ON whisper_log (campaign_id, delivered_at DESC);

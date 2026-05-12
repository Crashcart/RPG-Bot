-- =============================================================================
-- Migration 014: Whisper Protocol — Hidden Psychological State
-- =============================================================================
-- Adds the hidden_state JSONB column to the characters table and creates the
-- whisper_log audit table.  The hidden_state column stores psychological flags
-- (paranoia, cursed, horror_witness, etc.) and a running sanity score that
-- are never exposed to the player directly — only surfaced via GM whispers.
-- =============================================================================

-- Add hidden psychological state column to characters
ALTER TABLE characters
    ADD COLUMN IF NOT EXISTS hidden_state JSONB NOT NULL DEFAULT '{}';

-- GIN index for JSONB containment queries (e.g. flags @> '["paranoid"]')
CREATE INDEX IF NOT EXISTS idx_characters_hidden_state
    ON characters USING GIN (hidden_state);

-- Audit log for every whisper delivery
CREATE TABLE IF NOT EXISTS whisper_log (
    id            BIGSERIAL    PRIMARY KEY,
    character_id  UUID         NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    intent_id     UUID         NOT NULL,
    trigger       TEXT         NOT NULL,
    delta         JSONB        NOT NULL DEFAULT '{}',
    whisper_text  TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whisper_log_character_id
    ON whisper_log (character_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_whisper_log_intent_id
    ON whisper_log (intent_id);

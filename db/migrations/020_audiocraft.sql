-- Migration 020: AudioCraft latent acoustic memory
-- Stores per-location ambient audio seeds so a location sounds identical
-- across sessions (latent acoustic memory).

CREATE TABLE IF NOT EXISTS audiocraft_location_seeds (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    location_key    TEXT        NOT NULL,
    ambient_prompt  TEXT        NOT NULL,
    audio_url       TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, location_key)
);

CREATE INDEX IF NOT EXISTS idx_audiocraft_seeds_campaign
    ON audiocraft_location_seeds (campaign_id);

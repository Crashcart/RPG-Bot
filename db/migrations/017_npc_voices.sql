-- 017_npc_voices.sql
-- Persistent NPC voice model assignments for the Piper TTS pipeline (Issue #15).
--
-- Each NPC encountered across any campaign can be assigned a specific Piper
-- voice model that survives container restarts, ensuring consistent vocal
-- characterisation across sessions.
--
-- NOTE: Migration numbering — 012-016 are in open feature-branch PRs (not yet
-- merged to main). Use 017 to avoid conflicts with all currently-pending PRs.
--
-- Apply after 013_inference_settings.sql on main, or after whichever migration
-- is highest in the target environment.

BEGIN;

CREATE TABLE IF NOT EXISTS npc_voice_assignments (
    id              BIGSERIAL     PRIMARY KEY,
    npc_name_lower  TEXT          NOT NULL,
    campaign_id     UUID          NOT NULL
                        REFERENCES campaigns (id) ON DELETE CASCADE,
    voice_model_id  VARCHAR(128)  NOT NULL DEFAULT 'en_US-lessac-medium',
    -- Optional prosody overrides (1.0 = no change)
    pitch_scale     NUMERIC(4,2)  NOT NULL DEFAULT 1.0
                        CHECK (pitch_scale  BETWEEN 0.5 AND 2.0),
    speed_scale     NUMERIC(4,2)  NOT NULL DEFAULT 1.0
                        CHECK (speed_scale  BETWEEN 0.5 AND 2.0),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT uq_npc_voice_per_campaign
        UNIQUE (npc_name_lower, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_nvc_campaign
    ON npc_voice_assignments (campaign_id);

COMMENT ON TABLE npc_voice_assignments IS
    'Maps NPC names (case-folded) to Piper TTS voice model IDs per campaign. '
    'Populated on first NPC encounter; updated via /api/npc/voice endpoint.';

COMMIT;

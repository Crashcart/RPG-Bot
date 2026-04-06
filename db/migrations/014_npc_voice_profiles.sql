-- =============================================================================
-- Migration 014 – NPC Voice Profiles (Local Piper TTS Pipeline)
-- =============================================================================
-- Adds persistent voice synthesis profiles for NPCs and the GM Narrator.
--
-- Each NPC encountered in a campaign receives a voice_model_id, pitch, and
-- speed that are preserved across all future sessions.  The first time a
-- player meets an NPC the system auto-assigns a voice from available Piper
-- models; subsequent encounters use the same voice automatically.
--
-- Also seeds global_settings with Piper TTS service configuration that is
-- readable and editable via White Portal → Settings.
-- =============================================================================

-- ── NPC Voice Profiles ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS npc_voice_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    -- npc_name is normalised to lowercase for case-insensitive lookup
    npc_name        TEXT NOT NULL,
    -- Piper model identifier, e.g. "en_US-lessac-medium" or "en_GB-alan-medium"
    voice_model_id  TEXT NOT NULL DEFAULT 'en_US-lessac-medium',
    -- length_scale: 1.0 = normal. >1 = slower (more dramatic), <1 = faster (nervous)
    speed_scale     FLOAT NOT NULL DEFAULT 1.0
                        CHECK (speed_scale > 0.0 AND speed_scale <= 4.0),
    -- noise_scale controls phoneme duration variability (0.0–1.0)
    noise_scale     FLOAT NOT NULL DEFAULT 0.667
                        CHECK (noise_scale >= 0.0 AND noise_scale <= 1.0),
    -- noise_w controls pitch variability (0.0–1.0)
    noise_w         FLOAT NOT NULL DEFAULT 0.8
                        CHECK (noise_w >= 0.0 AND noise_w <= 1.0),
    -- Which TTS engine renders this NPC (piper | edge_tts | elevenlabs | openai_tts)
    tts_provider    TEXT NOT NULL DEFAULT 'piper',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, npc_name)
);

CREATE INDEX IF NOT EXISTS idx_npc_voice_profiles_campaign
    ON npc_voice_profiles(campaign_id);

CREATE TRIGGER trg_npc_voice_profiles_updated_at
    BEFORE UPDATE ON npc_voice_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE npc_voice_profiles IS
    'Persistent TTS voice parameters per NPC per campaign. '
    'Populated on first NPC encounter; ensures vocal consistency across sessions.';

-- ── Global Settings: Piper TTS ────────────────────────────────────────────────
INSERT INTO global_settings (key, value) VALUES
    -- URL of the Piper TTS HTTP service (docker-compose service: piper-tts on port 5500)
    ('piper_url',                    '"http://piper-tts:5500"'),
    -- Voice model used for the GM Narrator (non-NPC prose)
    ('piper_narrator_model',         '"en_US-lessac-medium"'),
    -- Default voice model assigned to new NPCs when none is specified
    ('piper_default_npc_model',      '"en_US-ryan-high"'),
    -- Whether the GM synthesis pass should emit [Speaker]: tags (enables diarization)
    ('piper_speaker_tags_enabled',   'true'),
    -- Update tts_provider to 'piper' to activate the local pipeline
    -- (existing 'edge_tts' | 'elevenlabs' | 'openai_tts' options remain valid)
    ('tts_provider',                 '"piper"')

ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

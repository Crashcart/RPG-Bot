-- =============================================================================
-- Migration 012 – Multimedia: Handouts, Factions, NPC Portraits,
--                  Scene Images, Music Feedback
-- =============================================================================

-- ── Handout type enum ─────────────────────────────────────────────────────────
CREATE TYPE handout_type AS ENUM (
    'letter',
    'journal',
    'map_note',
    'artifact_description',
    'rumour',
    'official_document',
    'coded_message',
    'general'
);

-- ── Handouts ──────────────────────────────────────────────────────────────────
CREATE TABLE handouts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id  UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    content_text TEXT NOT NULL,
    image_url    TEXT NOT NULL DEFAULT '',
    handout_type handout_type NOT NULL DEFAULT 'general',
    creator      TEXT NOT NULL DEFAULT 'gm',   -- 'gm' | player discord snowflake
    is_global    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_handouts_campaign ON handouts(campaign_id);

-- ── Handout delivery tracking ─────────────────────────────────────────────────
CREATE TABLE handout_recipients (
    handout_id   UUID NOT NULL REFERENCES handouts(id) ON DELETE CASCADE,
    player_id    TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (handout_id, player_id)
);
CREATE INDEX idx_handout_recipients_player ON handout_recipients(player_id);

-- ── Factions ──────────────────────────────────────────────────────────────────
-- disposition JSONB schema: { "player_snowflake_str": score_int }
-- score range: -100 (Enemy) to +100 (Allied)
CREATE TABLE factions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    disposition JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, name)
);
CREATE INDEX idx_factions_campaign ON factions(campaign_id);
CREATE TRIGGER trg_factions_updated_at
    BEFORE UPDATE ON factions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── NPC Portraits ─────────────────────────────────────────────────────────────
CREATE TABLE npc_portraits (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    npc_name     TEXT NOT NULL,
    campaign_id  UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    image_url    TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_id, npc_name)
);
CREATE INDEX idx_npc_portraits_campaign ON npc_portraits(campaign_id);

-- ── Scene Images ──────────────────────────────────────────────────────────────
CREATE TABLE scene_images (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id  UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    prompt       TEXT NOT NULL,
    image_url    TEXT NOT NULL,
    intent_id    UUID,     -- links to the action_log intent that triggered this image
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_scene_images_campaign ON scene_images(campaign_id);

-- ── Music Feedback ────────────────────────────────────────────────────────────
-- Records player thumbs-up / thumbs-down on generated music cues.
-- Used to enrich the idle prefetch and future generation prompts.
CREATE TABLE music_feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    original_prompt TEXT NOT NULL,
    audio_url       TEXT NOT NULL,
    approved        BOOLEAN,           -- TRUE=approved, FALSE=rejected, NULL=no feedback
    feedback_note   TEXT NOT NULL DEFAULT '',
    player_id       TEXT NOT NULL,     -- Discord snowflake string
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_music_feedback_campaign ON music_feedback(campaign_id);

-- Migration 014: NATS Bus — NPC Entity State Store
-- Issue #8: Multi-Agent Vector-Space Communication
--
-- Stores the last-known emotion hash and intent for each NPC entity.
-- Updated on every NpcReactionEvent received from the NATS bus.
-- Enables the GM Director to query NPC state without holding it in RAM.

BEGIN;

CREATE TABLE IF NOT EXISTS npc_entity_state (
    entity_id       UUID         NOT NULL,
    campaign_id     UUID         NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    entity_name     TEXT         NOT NULL,
    emotion_hash    INTEGER      NOT NULL DEFAULT 0,
    aggro_target    UUID,
    intended_action TEXT         NOT NULL DEFAULT 'idle',
    last_scene_id   TEXT         NOT NULL DEFAULT '',
    metadata        JSONB        NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_id, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_npc_entity_state_campaign
    ON npc_entity_state(campaign_id);

-- Partial index: fast lookup of hostile NPCs during combat
CREATE INDEX IF NOT EXISTS idx_npc_entity_state_aggro
    ON npc_entity_state(campaign_id, emotion_hash)
    WHERE emotion_hash = 1;

COMMENT ON TABLE npc_entity_state IS
    'Persisted NPC emotion/intent state updated from NATS NpcReactionEvents. '
    'emotion_hash values map to the EmotionHash enum in nats_schemas.py.';

COMMENT ON COLUMN npc_entity_state.emotion_hash IS
    '0=NEUTRAL 1=AGGRO 2=FEAR 3=CURIOUS 4=SUSPICIOUS 5=FRIENDLY '
    '6=PANIC 7=GUARD 8=INJURED 9=DEAD 10=CHARMED 11=STUNNED';

COMMIT;

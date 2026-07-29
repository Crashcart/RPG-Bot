-- Migration 014: Autonomous Background Entity Simulation (ABES)
-- Run: psql -U ironclad -d ironclad -f db/migrations/014_abes.sql
--
-- Adds three tables:
--   npc_long_term_intents  — background goals for world entities
--   world_delta            — event log written by each world tick
--   abes_config            — per-campaign ABES control knobs

-- ── Table: npc_long_term_intents ──────────────────────────────────────────────
-- Each row is one active background task for an NPC or faction.
-- The world-tick engine sweeps these rows, rolls dice, and updates them
-- without ever invoking an LLM.

CREATE TABLE IF NOT EXISTS npc_long_term_intents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,

    -- Entity being simulated (NPC name / faction tag / vehicle ID)
    entity_name     TEXT NOT NULL,
    entity_type     TEXT NOT NULL DEFAULT 'npc'
                        CHECK (entity_type IN ('npc', 'faction', 'vehicle', 'organization')),

    -- What the entity is trying to accomplish
    intent_type     TEXT NOT NULL
                        CHECK (intent_type IN (
                            'travel', 'smuggle', 'trade', 'repair',
                            'recruit', 'siege', 'patrol', 'rest', 'hunt'
                        )),
    description     TEXT NOT NULL,

    -- Mechanical stats used for dice checks
    skill_rating    INT  NOT NULL DEFAULT 10 CHECK (skill_rating BETWEEN 1 AND 20),
    hp              INT  NOT NULL DEFAULT 10 CHECK (hp > 0),
    max_hp          INT  NOT NULL DEFAULT 10 CHECK (max_hp > 0),

    -- Progress tracking: 0–100 percent complete
    progress        INT  NOT NULL DEFAULT 0  CHECK (progress BETWEEN 0 AND 100),

    -- Optional destination for travel/smuggle intents
    destination     TEXT NOT NULL DEFAULT '',

    -- How many ticks remain before the intent expires (NULL = unlimited)
    ticks_remaining INT  CHECK (ticks_remaining IS NULL OR ticks_remaining > 0),

    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'completed', 'failed', 'paused')),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_npc_intents_campaign
    ON npc_long_term_intents(campaign_id, status);

CREATE INDEX IF NOT EXISTS idx_npc_intents_entity
    ON npc_long_term_intents(campaign_id, entity_name);

-- ── Table: world_delta ────────────────────────────────────────────────────────
-- Immutable event log.  One row per significant background event.
-- GMDirector queries this for "catch-up" narrative generation when players
-- reconnect after an offline period.

CREATE TABLE IF NOT EXISTS world_delta (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,

    -- Which NPC/faction generated this event
    entity_name     TEXT NOT NULL,

    -- Short machine-readable event category for filtering
    event_type      TEXT NOT NULL
                        CHECK (event_type IN (
                            'progress', 'complication', 'death',
                            'task_complete', 'faction_shift', 'raid', 'discovery'
                        )),

    -- How significant this event is for narrative injection
    severity        TEXT NOT NULL DEFAULT 'minor'
                        CHECK (severity IN ('minor', 'major', 'critical')),

    -- One-sentence plain-English description (injected verbatim by GMDirector)
    summary         TEXT NOT NULL,

    -- Raw mechanical data (dice rolls, stat deltas) for audit
    mechanical_data JSONB NOT NULL DEFAULT '{}',

    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_world_delta_campaign_time
    ON world_delta(campaign_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_world_delta_severity
    ON world_delta(campaign_id, severity, occurred_at DESC);

-- ── Table: abes_config ────────────────────────────────────────────────────────
-- Per-campaign ABES control knobs.  One row per campaign.
-- All fields have sane defaults so the table is optional (created on first use).

CREATE TABLE IF NOT EXISTS abes_config (
    campaign_id         UUID PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,

    -- Enable/disable ABES ticks for this campaign
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,

    -- How often the world ticks (seconds between tick sweeps)
    tick_interval_s     INT NOT NULL DEFAULT 3600 CHECK (tick_interval_s >= 60),

    -- Time-dilation: multiplier for in-game time vs real time
    -- 1.0 = real-time, 2.0 = in-game time moves twice as fast
    time_dilation       FLOAT NOT NULL DEFAULT 1.0 CHECK (time_dilation > 0),

    -- Optional Discord webhook URL for major/critical event notifications
    webhook_url         TEXT NOT NULL DEFAULT '',

    -- How many world_delta rows to return for GMDirector catch-up (0 = all)
    catchup_limit       INT NOT NULL DEFAULT 20 CHECK (catchup_limit >= 0),

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

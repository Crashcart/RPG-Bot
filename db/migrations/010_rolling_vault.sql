-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 010: Rolling Vault — sliding context window for LLM overflow prevention
-- ─────────────────────────────────────────────────────────────────────────────
-- Implements the "Rolling Vault" from TDR Step 5.
-- Stores verbatim player/GM turns and Ollama-compressed summaries so the
-- ingestion phase always has a bounded, coherent history block.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS rolling_vault (
    id          BIGSERIAL    PRIMARY KEY,
    campaign_id UUID         NOT NULL,
    turn_seq    INT          NOT NULL,   -- monotonically increasing per campaign
    role        VARCHAR(16)  NOT NULL    CHECK (role IN ('player', 'gm', 'summary')),
    content     TEXT         NOT NULL,
    is_summary  BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Fast look-up by campaign ordered by sequence position
CREATE INDEX IF NOT EXISTS ix_rolling_vault_campaign_seq
    ON rolling_vault (campaign_id, turn_seq DESC);

-- Efficient summary retrieval (summaries are pinned and never expire)
CREATE INDEX IF NOT EXISTS ix_rolling_vault_summaries
    ON rolling_vault (campaign_id, is_summary)
    WHERE is_summary = TRUE;

-- Next-sequence helper: returns 1 + MAX(turn_seq) for a given campaign
-- (used by the application layer via SELECT)

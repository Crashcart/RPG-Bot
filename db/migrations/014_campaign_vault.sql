-- =============================================================================
-- Migration 014: Campaign Vault Registry
-- =============================================================================
-- Tracks which campaigns have a per-campaign SQLite vault file on disk,
-- their hibernation status, and last-accessed time.
--
-- The actual vault files live at:
--   /app/data/vault/campaign_<campaign_id>.db
-- and are managed by orchestrator/services/campaign_vault.py.
-- =============================================================================

BEGIN;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vault_status') THEN
        CREATE TYPE vault_status AS ENUM ('active', 'hibernating', 'hibernated');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS campaign_vaults (
    campaign_id     UUID            PRIMARY KEY
                                    REFERENCES campaigns(id) ON DELETE CASCADE,
    vault_path      TEXT            NOT NULL,
    status          vault_status    NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_accessed   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    hibernated_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_campaign_vaults_status
    ON campaign_vaults(status);

CREATE INDEX IF NOT EXISTS idx_campaign_vaults_last_accessed
    ON campaign_vaults(last_accessed);

COMMENT ON TABLE campaign_vaults IS
    'Registry of per-campaign SQLite vault files. '
    'Actual .db files live at /app/data/vault/campaign_<id>.db';

COMMENT ON COLUMN campaign_vaults.vault_path IS
    'Relative path from /app/data — e.g. vault/campaign_<uuid>.db';

COMMENT ON COLUMN campaign_vaults.status IS
    'active=worker running, hibernating=flush in progress, hibernated=worker stopped';

COMMIT;

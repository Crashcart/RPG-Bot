-- Migration 014: Multi-Tenant Campaign Vault tracking
-- Run: psql -U ironclad -d ironclad -f db/migrations/014_campaign_vault.sql
--
-- Adds a `campaign_vaults` table so the central PostgreSQL database can track
-- which campaigns have had an isolated SQLite vault provisioned for them, when
-- that happened, and the current hibernation / cold-start status.
--
-- The actual per-campaign data lives in individual SQLite files at:
--   /app/data/vault/campaigns/campaign_<uuid>.db
-- This table is the registry/index; the SQLite file is the source of truth.

-- ── campaign_vaults ───────────────────────────────────────────────────────────
-- One row per provisioned multi-tenant campaign vault.
CREATE TABLE IF NOT EXISTS campaign_vaults (
    campaign_id     UUID        PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
    -- Absolute path of the SQLite database file on the server filesystem.
    db_path         TEXT        NOT NULL,
    -- Current lifecycle status of the isolated worker process.
    status          TEXT        NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'hibernated', 'destroyed')),
    -- UTC timestamp of when the vault was first provisioned.
    provisioned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- UTC timestamp of the most recent player action routed to this vault.
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- UTC timestamp when the vault entered hibernation (NULL if active).
    hibernated_at   TIMESTAMPTZ DEFAULT NULL,
    -- Human-readable display name, mirrored from the SQLite campaign_meta table
    -- for convenient querying without opening the SQLite file.
    display_name    TEXT        NOT NULL DEFAULT '',
    -- Active world/genre at provisioning time.
    world           TEXT        NOT NULL DEFAULT '',
    -- Arbitrary metadata: guild_id, system, tags, etc.
    metadata        JSONB       NOT NULL DEFAULT '{}'
);

-- Quickly find all non-destroyed vaults for housekeeping / hibernation jobs.
CREATE INDEX IF NOT EXISTS idx_campaign_vaults_status
    ON campaign_vaults(status);

-- Quickly find stale vaults that should be hibernated (last_active_at < threshold).
CREATE INDEX IF NOT EXISTS idx_campaign_vaults_last_active
    ON campaign_vaults(last_active_at);

-- ── Auto-update last_active_at ─────────────────────────────────────────────
-- Bump last_active_at whenever the vault row is touched (e.g. when the
-- pipeline sets status back to 'active' after a cold start).
CREATE OR REPLACE FUNCTION refresh_vault_last_active()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'active' AND OLD.status = 'hibernated' THEN
        NEW.last_active_at = NOW();
        NEW.hibernated_at  = NULL;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_vault_wake ON campaign_vaults;
CREATE TRIGGER trg_vault_wake
    BEFORE UPDATE ON campaign_vaults
    FOR EACH ROW EXECUTE FUNCTION refresh_vault_last_active();

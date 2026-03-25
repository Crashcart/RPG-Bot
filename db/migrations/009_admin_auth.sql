-- Migration 009: White Portal — First-Boot Admin Authentication
-- Adds the admin_accounts table for the first-boot security lock.
--
-- On initial launch, if admin_accounts is empty the orchestrator
-- redirects ALL /web/ requests to /web/setup so an admin password
-- must be created before any panel page is accessible.

BEGIN;

CREATE TABLE IF NOT EXISTS admin_accounts (
    id            SERIAL      PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    password_hash TEXT        NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE admin_accounts IS
    'White Portal admin credentials. First-boot check: if empty → lock all /web/ routes.';

COMMIT;

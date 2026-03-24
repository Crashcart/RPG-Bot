-- =============================================================================
-- Migration 004 – Role-Based Node Routing + System Settings
-- =============================================================================

-- ── Role tags on every node ───────────────────────────────────────────────────
-- roles is a JSONB array of strings, e.g.:
--   ["adjudication"]
--   ["narrative", "scribe"]
--   ["vision", "adjudication"]
--
-- Well-known roles:
--   adjudication  – Phase 2 mechanical resolution (any Ollama node can fill this)
--   narrative     – Phase 4 local storyteller (used when Cloud Storyteller is OFF)
--   scribe        – Background lore fact extraction / DB writing
--   vision        – Vision/OCR tasks (future)
--   code_gen      – Code generation tasks (future)
--
-- A node without any roles assigned will still receive adjudication requests
-- through the standard priority-ordered fallback path.

ALTER TABLE node_registry
    ADD COLUMN IF NOT EXISTS roles JSONB NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS idx_node_registry_roles_gin
    ON node_registry USING GIN (roles);

-- ── Global System Settings ────────────────────────────────────────────────────
-- Stores operator-level toggles and configuration that apply across all
-- campaigns and sessions.  Uses JSONB values so any primitive or structure
-- can be stored without schema changes.
--
-- Current keys:
--   storyteller_api_enabled  BOOL   true  → use Gemini for Phase 4 narrative
--                                    false → promote highest-priority local
--                                            Ollama node with role 'narrative'

CREATE TABLE IF NOT EXISTS system_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT 'null',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION set_system_settings_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_system_settings_updated_at
    BEFORE UPDATE ON system_settings
    FOR EACH ROW EXECUTE FUNCTION set_system_settings_updated_at();

-- Seed defaults (idempotent)
INSERT INTO system_settings (key, value)
VALUES ('storyteller_api_enabled', 'true')
ON CONFLICT (key) DO NOTHING;

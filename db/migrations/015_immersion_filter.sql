-- Migration 015: Per-campaign immersion filter settings
-- Enables the ImmersionFilter to be configured per-campaign from the
-- White Portal admin panel (Settings → Immersion).
--
-- Applies to: Issue #12 — Immersion Enforcement & Dynamic UI Middleware

CREATE TABLE IF NOT EXISTS campaign_immersion_settings (
    campaign_id       UUID        PRIMARY KEY
                                  REFERENCES campaigns(id) ON DELETE CASCADE,
    custom_blocklist  TEXT[]      NOT NULL DEFAULT '{}',
    censor_reversion  BOOLEAN     NOT NULL DEFAULT TRUE,
    flatten_lists     BOOLEAN     NOT NULL DEFAULT TRUE,
    brand_filter      BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  campaign_immersion_settings
    IS 'Per-campaign ImmersionFilter configuration (Issue #12).';
COMMENT ON COLUMN campaign_immersion_settings.custom_blocklist
    IS 'Campaign-specific brand / entity names appended to the seed BRAND_BLOCKLIST.';
COMMENT ON COLUMN campaign_immersion_settings.censor_reversion
    IS 'Enable Pass 1: asterisk censorship-symbol reversion.';
COMMENT ON COLUMN campaign_immersion_settings.flatten_lists
    IS 'Enable Pass 2: markdown list and table flattening.';
COMMENT ON COLUMN campaign_immersion_settings.brand_filter
    IS 'Enable Pass 3: brand-name nullification.';

-- Function: retrieve settings with defaults if no row exists.
CREATE OR REPLACE FUNCTION get_campaign_immersion_settings(p_campaign_id UUID)
RETURNS TABLE (
    custom_blocklist TEXT[],
    censor_reversion BOOLEAN,
    flatten_lists    BOOLEAN,
    brand_filter     BOOLEAN
) LANGUAGE sql STABLE AS $$
    SELECT
        COALESCE(s.custom_blocklist, '{}'),
        COALESCE(s.censor_reversion, TRUE),
        COALESCE(s.flatten_lists,    TRUE),
        COALESCE(s.brand_filter,     TRUE)
    FROM (SELECT p_campaign_id AS id) AS ref
    LEFT JOIN campaign_immersion_settings s ON s.campaign_id = ref.id;
$$;

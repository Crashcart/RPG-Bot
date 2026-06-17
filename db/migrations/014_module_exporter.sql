-- Migration 014: Campaign Module Exporter (Issue #25 — Make & Take)
-- Run: psql -U ironclad -d ironclad -f db/migrations/014_module_exporter.sql

CREATE TYPE export_job_status AS ENUM ('queued', 'running', 'complete', 'failed');

CREATE TABLE export_jobs (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id    UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    status         export_job_status NOT NULL DEFAULT 'queued',
    sanitized      BOOLEAN     NOT NULL DEFAULT TRUE,
    include_media  BOOLEAN     NOT NULL DEFAULT TRUE,
    archive_path   TEXT,
    error_detail   TEXT,
    manifest       JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ
);

CREATE INDEX idx_export_jobs_campaign ON export_jobs(campaign_id);
CREATE INDEX idx_export_jobs_status   ON export_jobs(status);
CREATE INDEX idx_export_jobs_created  ON export_jobs(created_at DESC);

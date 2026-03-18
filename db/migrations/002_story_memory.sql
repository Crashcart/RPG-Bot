-- =============================================================================
-- Migration 002 – Story Memory & Continuity Tables
-- Gives the GM a persistent world-state it can consult before narrating,
-- preventing hallucinations, contradictions, and continuity errors.
-- =============================================================================

-- ── story_context ─────────────────────────────────────────────────────────────
-- One row per established world fact, NPC, location, event, or plot thread.
-- Updated in-place when the same entity is mentioned again.
CREATE TABLE IF NOT EXISTS story_context (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id         UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    -- Semantic type for filtering / display
    entity_type         TEXT NOT NULL CHECK (entity_type IN (
                            'npc', 'location', 'event', 'world_fact', 'plot_thread'
                        )),
    entity_name         TEXT NOT NULL,       -- Canonical name / label
    summary             TEXT NOT NULL,       -- One-sentence fact (fed into every prompt)
    detail              TEXT NOT NULL DEFAULT '',  -- Expanded detail (stored, retrieved by RAG)
    -- ChromaDB reference so embeddings stay in sync
    chroma_doc_id       TEXT,
    -- Provenance
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_intent_id    UUID                 -- Which player action established this fact
);

CREATE INDEX IF NOT EXISTS idx_story_context_campaign
    ON story_context(campaign_id);
CREATE INDEX IF NOT EXISTS idx_story_context_type
    ON story_context(campaign_id, entity_type);
-- Unique constraint: one canonical record per entity name per campaign
CREATE UNIQUE INDEX IF NOT EXISTS uq_story_context_entity
    ON story_context(campaign_id, entity_name);
-- Full-text index on summary for keyword fallback search
CREATE INDEX IF NOT EXISTS idx_story_context_summary_fts
    ON story_context USING GIN (to_tsvector('english', summary || ' ' || detail));

-- ── Trigger ───────────────────────────────────────────────────────────────────
CREATE TRIGGER trg_story_context_updated_at
    BEFORE UPDATE ON story_context
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

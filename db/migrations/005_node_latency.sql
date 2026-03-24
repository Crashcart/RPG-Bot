-- =============================================================================
-- Migration 005 – Node Latency Benchmarking Columns
-- =============================================================================
-- Stores the result of the most recent Time to First Token (TTFT) heartbeat
-- probe for each registered AI node.
--
-- latency_ms            – TTFT in milliseconds from the last benchmark run.
--                         NULL means the node has not been benchmarked yet
--                         (e.g. newly added, or all benchmark attempts failed).
-- latency_measured_at   – Wall-clock timestamp of the last successful benchmark.
--
-- These are written by the NodeRouter health loop (every 30 seconds) and read
-- by the Auto-Promotion Protocol to select the fastest available node when the
-- Cloud Storyteller is toggled off.

ALTER TABLE node_registry
    ADD COLUMN IF NOT EXISTS latency_ms         INTEGER,
    ADD COLUMN IF NOT EXISTS latency_measured_at TIMESTAMPTZ;

-- Index lets the router sort by latency efficiently.
-- NULLS LAST puts unbenchmarked nodes below any real measurement.
CREATE INDEX IF NOT EXISTS idx_node_registry_latency
    ON node_registry (latency_ms ASC NULLS LAST)
    WHERE enabled = TRUE;

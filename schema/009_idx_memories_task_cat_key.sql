-- Migration: composite index for the worker's dedupe lookup
-- Replaces the old regexp_replace-based table scan with an indexable expression.
-- Applied after the worker.py change to use lower(key) = lower($3).
CREATE INDEX IF NOT EXISTS idx_memories_task_cat_key
    ON proxy.memories (task_id, category, lower(key));

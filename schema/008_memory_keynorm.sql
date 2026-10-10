-- 008_memory_keynorm.sql
-- Adds a precomputed, indexable normalized-key column so the 4B worker's
-- dedupe query (task_id, category, key_norm) is an index lookup instead of a
-- full table scan. key_norm mirrors worker._norm(): lowercase, non-alnum->space,
-- whitespace collapsed. This also fixes a correctness bug: the old query
-- compared lower(key) [raw title] to lower(_norm(title)), which never matched
-- for titles with punctuation, so dedup silently failed.
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS key_norm TEXT;

-- Backfill existing rows (idempotent).
UPDATE proxy.memories
SET key_norm = trim(regexp_replace(regexp_replace(lower(key), '[^a-z0-9]+', ' ', 'g'), '[[:space:]]+', ' ', 'g'))
WHERE key_norm IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_keynorm
    ON proxy.memories (task_id, category, key_norm) WHERE active = true;

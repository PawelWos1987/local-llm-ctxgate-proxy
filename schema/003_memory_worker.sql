-- 003: columns used by the 4B memory worker (source_event_id, status)
-- Keeps existing 'active' boolean for backward compatibility with tests.
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS source_event_id UUID;
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
CREATE INDEX IF NOT EXISTS idx_memories_status
    ON proxy.memories (task_id, active, importance) WHERE active = true;

-- Outage tracking (added for CTXGATE_WORKER_OUTAGE_TTL feature)
ALTER TABLE proxy.memory_jobs ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;

-- model_name: which 4B model produced each memory (worker INSERTs this column)
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS model_name TEXT;

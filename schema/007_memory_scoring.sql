-- 007_memory_scoring.sql
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS score DOUBLE PRECISION DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_memories_score ON proxy.memories (task_id, active, score DESC) WHERE active = true;

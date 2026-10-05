-- 004_memory_ttl.sql: add TTL / last-access tracking to proxy.memories
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
ALTER TABLE proxy.memories ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed ON proxy.memories (last_accessed_at) WHERE active;
CREATE INDEX IF NOT EXISTS idx_memories_expires ON proxy.memories (expires_at) WHERE expires_at IS NOT NULL;

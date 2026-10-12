ALTER TABLE proxy.memory_jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_jobs_next_retry ON proxy.memory_jobs(next_retry_at) WHERE next_retry_at IS NOT NULL;

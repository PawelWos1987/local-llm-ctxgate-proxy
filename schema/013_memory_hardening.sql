-- 013_memory_hardening.sql: durable-memory hardening for the 4B worker
-- Idempotent: safe to re-run. Applied after 012_session_windows.sql.
--
-- Adds:
--   1. claimed_at on proxy.memory_jobs — timestamp when a consumer claimed
--      the job (set by claim_jobs). Used by recover_stuck_jobs() to detect
--      jobs stuck in 'processing' after a worker crash.
--   2. Index on claimed_at for efficient stuck-job recovery queries.
--   3. Index on (task_id, source_event_id) for duplicate detection.
--
-- Apply:
--   PGPASSWORD=<pw> psql -h 127.0.0.1 -p 5432 -U postgres -d ctxproxy -f schema/013_memory_hardening.sql

-- 1. claimed_at: when a consumer picked up the job (for stuck-job recovery)
ALTER TABLE proxy.memory_jobs ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

-- 2. Index for stuck-job recovery (WHERE status='processing' AND claimed_at < ...)
CREATE INDEX IF NOT EXISTS idx_memory_jobs_claimed
    ON proxy.memory_jobs (claimed_at)
    WHERE status = 'processing';

-- 3. Index for duplicate detection (same task + source event)
CREATE INDEX IF NOT EXISTS idx_memories_task_event
    ON proxy.memories (task_id, source_event_id)
    WHERE active = true;

-- 011_phase_summaries.sql
-- Hierarchical/chaptered summarization: each trim event creates a "phase" summary.
-- The root summary (session_summaries) is a distillation of all phase summaries.
CREATE TABLE IF NOT EXISTS proxy.phase_summaries (
    id SERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    session_key VARCHAR(200),
    phase_number INT NOT NULL,
    summary TEXT NOT NULL,
    trimmed_msg_count INT DEFAULT 0,
    trimmed_tokens INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE(task_id, phase_number)
);
CREATE INDEX IF NOT EXISTS idx_phase_summaries_task ON proxy.phase_summaries(task_id, phase_number);


-- local-llm-ctxgate-proxy schema: proxy
-- 5 tables: tasks, events, memories, working_memory, memory_jobs

CREATE TABLE IF NOT EXISTS proxy.tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS proxy.events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES proxy.tasks(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,  -- system, user, assistant, tool
    content         TEXT NOT NULL DEFAULT '',
    reasoning       TEXT,
    tool_calls      JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON proxy.events (task_id, seq);

CREATE TABLE IF NOT EXISTS proxy.memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES proxy.tasks(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'general',
    importance      INTEGER NOT NULL DEFAULT 5,  -- 1-10
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by   UUID REFERENCES proxy.memories(id),
    active          BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_memories_task ON proxy.memories (task_id, active);
CREATE INDEX IF NOT EXISTS idx_memories_gin ON proxy.memories USING gin ((key || ' ' || value) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS proxy.working_memory (
    task_id         UUID PRIMARY KEY REFERENCES proxy.tasks(id) ON DELETE CASCADE,
    content         TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS proxy.memory_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES proxy.tasks(id) ON DELETE CASCADE,
    event_id        UUID REFERENCES proxy.events(id),
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending, processing, done, failed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    result          JSONB,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_jobs_pending ON proxy.memory_jobs (status, created_at) WHERE status = 'pending';


"""Database migrations: DDL statements for table and index creation.

These statements are executed during the lifespan startup in app.py.
This module centralizes them for reference and testing.
"""

MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS proxy.session_windows (
        session_key TEXT PRIMARY KEY,
        cut INT NOT NULL DEFAULT 0,
        cut_anchor TEXT,
        cut_prev_anchor TEXT,
        seed_sig TEXT,
        summarized_through INT NOT NULL DEFAULT 0,
        dropped_total INT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.events (
        id BIGSERIAL PRIMARY KEY,
        task_uuid UUID NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        meta JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.tasks (
        task_uuid UUID PRIMARY KEY,
        session_id TEXT,
        tenant_id TEXT,
        title TEXT,
        state TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.memories (
        id BIGSERIAL PRIMARY KEY,
        task_id UUID NOT NULL,
        tenant_id TEXT,
        key TEXT NOT NULL,
        value TEXT,
        category TEXT,
        importance TEXT NOT NULL DEFAULT 'NORMAL',
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.knowledge (
        id BIGSERIAL PRIMARY KEY,
        tenant_id TEXT,
        domain TEXT,
        key TEXT NOT NULL,
        value TEXT,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.working_memory (
        id BIGSERIAL PRIMARY KEY,
        task_id UUID NOT NULL,
        tenant_id TEXT,
        content TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.session_summaries (
        id BIGSERIAL PRIMARY KEY,
        task_id UUID NOT NULL,
        tenant_id TEXT,
        content TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.phase_summaries (
        id BIGSERIAL PRIMARY KEY,
        task_id UUID NOT NULL,
        tenant_id TEXT,
        content TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.session_ledger (
        id BIGSERIAL PRIMARY KEY,
        task_id UUID NOT NULL,
        tenant_id TEXT,
        kind TEXT NOT NULL,
        title TEXT,
        detail TEXT,
        evidence TEXT,
        source TEXT,
        path TEXT,
        fn_name TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS proxy.deliverables (
        id BIGSERIAL PRIMARY KEY,
        tenant_id TEXT,
        title TEXT,
        content TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_task ON proxy.events (task_uuid, id)",
    "CREATE INDEX IF NOT EXISTS idx_events_created ON proxy.events (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON proxy.tasks (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON proxy.tasks (tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_task_active ON proxy.memories (task_id, active)",
    "CREATE INDEX IF NOT EXISTS idx_memories_task_active_importance ON proxy.memories (task_id, active, importance)",
    "CREATE INDEX IF NOT EXISTS idx_memories_tenant_task ON proxy.memories (tenant_id, task_id, active)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_active ON proxy.knowledge (active)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_tenant ON proxy.knowledge (tenant_id, active)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_tenant_domain_key_active ON proxy.knowledge (tenant_id, domain, key) WHERE active",
    "CREATE INDEX IF NOT EXISTS idx_working_memory_task ON proxy.working_memory (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_working_memory_tenant ON proxy.working_memory (tenant_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_session_summaries_task ON proxy.session_summaries (task_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_session_summaries_tenant ON proxy.session_summaries (tenant_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_phase_summaries_task ON proxy.phase_summaries (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_phase_summaries_tenant ON proxy.phase_summaries (tenant_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_session_ledger_task ON proxy.session_ledger (task_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_session_ledger_task_id ON proxy.session_ledger(task_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_session_ledger_tenant ON proxy.session_ledger (tenant_id, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_deliverables_tenant ON proxy.deliverables (tenant_id)",
]


async def run_migrations(pool) -> None:
    """Execute all migration statements against the given pool."""
    for stmt in MIGRATIONS:
        await pool.execute(stmt)
    for stmt in INDEXES:
        await pool.execute(stmt)

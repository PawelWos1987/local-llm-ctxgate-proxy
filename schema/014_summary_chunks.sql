-- Migration 014: Per-chunk summary state for restart-safe summarization
-- Tracks individual chunk success/failure so the watermark only advances
-- when ALL chunks for a range have succeeded.
--
-- Phase 07 enhancements: full state machine, lease-based ownership,
-- source provenance, tenant scoping, and tool body archive.

CREATE TABLE IF NOT EXISTS proxy.summary_chunks (
    id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    session_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT '',
    chunk_id TEXT NOT NULL,  -- deterministic: sha256(task_id || start_idx || end_idx || chunk_seq)
    start_idx INT NOT NULL,
    end_idx INT NOT NULL,
    chunk_seq INT NOT NULL,
    -- Phase 07: Full state machine
    state TEXT NOT NULL DEFAULT 'queued',
        -- queued | claimed | processing | stored | retryable_failure | terminal_failure
    attempts INT NOT NULL DEFAULT 0,
    owner TEXT,              -- worker identifier
    lease_expires TIMESTAMPTZ,
    -- Source provenance
    source_range_start INT NOT NULL DEFAULT 0,  -- start_event_id
    source_range_end INT NOT NULL DEFAULT 0,    -- end_event_id
    source_hash TEXT NOT NULL DEFAULT '',       -- SHA-256 of source content
    chunk_policy_version INT NOT NULL DEFAULT 1,
    -- Output
    summary TEXT,
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(task_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_summary_chunks_task ON proxy.summary_chunks(task_id, state);
CREATE INDEX IF NOT EXISTS idx_summary_chunks_session ON proxy.summary_chunks(tenant_id, session_key, state);
CREATE INDEX IF NOT EXISTS idx_summary_chunks_lease ON proxy.summary_chunks(state, lease_expires);

-- Phase 07: Tool body archive for large tool outputs
-- Stores elided tool content in bounded chunks for range retrieval.
CREATE TABLE IF NOT EXISTS proxy.tool_body_chunks (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    event_hash TEXT NOT NULL,        -- SHA-256 of the original tool content (first 16 hex chars)
    chunk_idx INT NOT NULL,          -- 0-based chunk index
    content TEXT NOT NULL,           -- chunk content (bounded by TOOL_BODY_CHUNK_CHARS)
    content_hash TEXT NOT NULL,      -- SHA-256 of this chunk's content
    total_chunks INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(session_key, event_hash, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_tool_body_lookup ON proxy.tool_body_chunks(tenant_id, session_key, event_hash, chunk_idx);

-- Phase 06: Enhanced ledger with structured records
CREATE TABLE IF NOT EXISTS proxy.ledger_entries (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    task_id UUID,
    -- Structured fields
    entry_type TEXT NOT NULL,        -- requirement | constraint | decision | rejected_approach | completed_work | test_result | failure | artifact | todo
    status TEXT NOT NULL DEFAULT 'active',  -- active | completed | superseded | rejected | uncertain
    source_event_id BIGINT,          -- provenance: event ID
    source_hash TEXT NOT NULL DEFAULT '',  -- provenance: content hash
    source_span TEXT,                -- provenance: span descriptor
    text TEXT NOT NULL,             -- the instruction/content
    explicit BOOLEAN NOT NULL DEFAULT FALSE,  -- true if user explicitly stated
    superseded_by BIGINT,           -- id of the entry that superseded this one
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ledger_session ON proxy.ledger_entries(tenant_id, session_key, status);
CREATE INDEX IF NOT EXISTS idx_ledger_task ON proxy.ledger_entries(task_id, status);
CREATE INDEX IF NOT EXISTS idx_ledger_source ON proxy.ledger_entries(session_key, source_hash);

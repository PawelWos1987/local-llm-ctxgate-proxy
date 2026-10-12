-- 015_tool_body_chunks.sql
-- Durable archive for large tool output bodies.
-- Enables bounded range retrieval of tool results that exceed context budget.
-- Tenant/session scoped.

CREATE SCHEMA IF NOT EXISTS proxy;

CREATE TABLE IF NOT EXISTS proxy.tool_body_chunks (
    id              BIGSERIAL PRIMARY KEY,
    session_key     TEXT NOT NULL,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    event_hash      TEXT NOT NULL,          -- SHA-256 of the original tool result content
    chunk_idx       INTEGER NOT NULL,       -- 0-based chunk index
    content         TEXT NOT NULL,          -- chunk content (bounded size)
    content_hash    TEXT NOT NULL,          -- SHA-256 of this chunk
    total_chunks    INTEGER NOT NULL,       -- total number of chunks for this event
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_key, event_hash, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_tool_body_lookup
    ON proxy.tool_body_chunks (session_key, event_hash, chunk_idx);

CREATE INDEX IF NOT EXISTS idx_tool_body_tenant
    ON proxy.tool_body_chunks (tenant_id, session_key);


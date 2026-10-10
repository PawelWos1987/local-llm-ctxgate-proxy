
-- local-llm-ctxgate-proxy schema: proxy.knowledge (cross-session, global)
-- Unlike proxy.memories (per-task FK), knowledge is GLOBAL
-- enabling sharing across all sessions.

CREATE TABLE IF NOT EXISTS proxy.knowledge (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain          TEXT NOT NULL DEFAULT 'general',   -- architecture, debugging, config, fact, decision, preference
    key             TEXT NOT NULL,                     -- short identifier (e.g., "proxy_max_input", "qwen_min_p")
    value           TEXT NOT NULL,                     -- the knowledge content
    importance      INTEGER NOT NULL DEFAULT 5,        -- 1-10
    source_session  TEXT,                              -- which X-Session-ID created this
    source_key      TEXT,                              -- which session_key (fp) created this
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    active          BOOLEAN NOT NULL DEFAULT true
);

-- Unique constraint: one knowledge item per (domain, key)
-- Upsert on conflict: update value + importance
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_domain_key
    ON proxy.knowledge (domain, key) WHERE active = true;

-- Search index for full-text matching
CREATE INDEX IF NOT EXISTS idx_knowledge_search
    ON proxy.knowledge USING gin ((key || ' ' || value) gin_trgm_ops);

-- Relevance ranking: importance desc, recent first
CREATE INDEX IF NOT EXISTS idx_knowledge_relevance
    ON proxy.knowledge (active, importance DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS proxy.deliverables (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    session_type    TEXT NOT NULL DEFAULT 'goose',
    working_dir     TEXT NOT NULL DEFAULT '',
    provider_name   TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    document_path   TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_deliverables_created ON proxy.deliverables (created_at DESC);
CREATE INDEX idx_deliverables_session ON proxy.deliverables (session_type, working_dir);

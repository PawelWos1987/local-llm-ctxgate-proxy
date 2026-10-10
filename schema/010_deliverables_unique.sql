-- 010: unique (working_dir, document_path) on proxy.deliverables
-- Enables idempotent ON CONFLICT upsert in POST /api/deliverable so an agent
-- re-registering the same document refreshes the row instead of duplicating it.
-- Safe: the table is new (feature introduced in this change) and starts empty.
CREATE UNIQUE INDEX IF NOT EXISTS uq_deliverables_doc
    ON proxy.deliverables (working_dir, document_path);

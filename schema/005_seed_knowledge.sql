
-- Seed knowledge table with relevant ctxproxy knowledge
INSERT INTO proxy.knowledge (domain, key, value, importance, source_session, source_key, updated_at)
VALUES 
    ('config', 'proxy_port', 'ctxgate-proxy runs on port 9201 (CTXGATE_PROXY_PORT=9201)', 8, '20261002_17', '20261002_17:00000000', now()),
    ('config', 'vllm_endpoint', 'vLLM serves Qwen3.8-27B at http://127.0.0.1:29000/v1 with 130k context window', 8, '20261002_17', '20261002_17:00000000', now()),
    ('config', '4b_model', '4B memory worker uses qwen3-4b-instruct-2507 via LM Studio at http://127.0.0.1:1234/v1', 7, '20261002_17', '20261002_17:00000000', now()),
    ('architecture', 'session_isolation', 'Each Goose session gets a unique proxy.tasks row keyed by session_id; memories and summaries are per-task', 9, '20261002_17', '20261002_17:00000000', now()),
    ('architecture', 'context_trimming', 'FIFO trim protects system + newest user message; oldest messages dropped first; trimmed messages summarized by 4B', 8, '20261002_17', '20261002_17:00000000', now()),
    ('decision', 'worker_separation', 'Memory extraction handled by dedicated worker.py process (not app.py inline loop) to avoid race conditions', 9, '20261002_17', '20261002_17:00000000', now()),
    ('fact', 'goose_db_location', 'Goose sessions stored in SQLite at /home/user/.local/share/goose/sessions/sessions.db', 7, '20261002_17', '20261002_17:00000000', now()),
    ('config', 'max_context', 'CTXGATE_MAX_CONTEXT=84000, MAX_INPUT=64000, MAX_OUTPUT=18000, SAFETY_MARGIN=2000', 7, '20261002_17', '20261002_17:00000000', now())
ON CONFLICT (domain, key) WHERE active = true
DO UPDATE SET value = EXCLUDED.value, importance = GREATEST(EXCLUDED.importance, proxy.knowledge.importance), updated_at = now();

SELECT domain, key, importance FROM proxy.knowledge WHERE active = true ORDER BY importance DESC;

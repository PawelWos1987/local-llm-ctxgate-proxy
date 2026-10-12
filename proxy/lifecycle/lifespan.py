"""FastAPI lifespan handler: startup and shutdown logic."""
import asyncio
from proxy.window.persist import _flush_pending_persists, _periodic_flush_loop
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

import asyncpg
import httpx

log = logging.getLogger("ctxgate-proxy")


@asynccontextmanager
async def lifespan(app):
    from fastapi import FastAPI
    from proxy.app import (
        pool, enc, _api_key_map, _parse_api_keys, validate_config,
        CTXGATE_ALLOW_NO_AUTH, DB_DSN, ESTIMATOR_MODE, QWEN_TOKENIZER_PATH,
        RHO_COLD, VLLM_URL, VLLM_MODEL, MISTRAL_URL, MISTRAL_MODEL,
        MISTRAL_TIMEOUT, MISTRAL_API_KEY, CTXGATE_LM_ENABLED,
        _env_int, _memory_worker_loop, _fd_hygiene_loop, _watchdog_loop,
        _sd_notify, _lm_consumer, _restart_recovery,
    )
    from proxy.lm.rate_limiter import MistralRateLimiter
    import tokenizers as _tokenizers_mod

    global _hygiene_task
    import proxy.app as _app

    _lc_problems = validate_config()
    if _lc_problems:
        sys.exit(78)
    _app._api_key_map = _parse_api_keys()
    if _app._api_key_map:
        log.info("Auth: %d API keys loaded (labels: %s)", len(_app._api_key_map), ",".join(sorted(set(_app._api_key_map.values()))))
    if CTXGATE_ALLOW_NO_AUTH and not _app._api_key_map:
        log.warning("WARNING: Running with NO AUTHENTICATION. All routes accessible. NEVER use behind a reverse proxy.")

    # Retry DB connection
    for _attempt in range(60):
        try:
            _app.pool = await asyncpg.create_pool(
                DB_DSN, min_size=2, max_size=10,
                command_timeout=10, timeout=10,
                max_inactive_connection_lifetime=300,
            )
            break
        except Exception as _e:
            log.warning("DB not ready (attempt %d/60): %s - retrying in 2s", _attempt + 1, _e)
            await asyncio.sleep(2)
            if _attempt == 59:
                raise RuntimeError("Cannot connect to PostgreSQL after 120s") from _e
    log.info("DB pool connected")

    # Phase 3: additive migrations
    try:
        _ddl = ("""
            CREATE TABLE IF NOT EXISTS proxy.session_ledger (
                id BIGSERIAL PRIMARY KEY,
                task_id UUID NOT NULL,
                kind TEXT NOT NULL,
                title TEXT,
                detail TEXT,
                evidence TEXT,
                status TEXT DEFAULT 'active',
                source TEXT,
                slice_start INT,
                slice_end INT,
                phase_number INT,
                dedupe_hash TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE(task_id, dedupe_hash)
            )""")
        await _app.pool.execute(_ddl)
        await _app.pool.execute("CREATE INDEX IF NOT EXISTS idx_ledger_task_id ON proxy.session_ledger(task_id, id)")
        await _app.pool.execute("ALTER TABLE proxy.phase_summaries ADD COLUMN IF NOT EXISTS slice_start INT")
        await _app.pool.execute("ALTER TABLE proxy.phase_summaries ADD COLUMN IF NOT EXISTS slice_end INT")
        await _app.pool.execute("ALTER TABLE proxy.phase_summaries ADD COLUMN IF NOT EXISTS chunk_idx INT")
        await _app.pool.execute("ALTER TABLE proxy.events ADD COLUMN IF NOT EXISTS meta jsonb")
        log.info("Phase 3+5 migrations applied")
        _phase1_indexes = [
            ("idx_tasks_session_id", "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON proxy.tasks(session_id)"),
            ("idx_memories_task_active", "CREATE INDEX IF NOT EXISTS idx_memories_task_active ON proxy.memories(task_id, active)"),
            ("idx_memories_task_active_importance", "CREATE INDEX IF NOT EXISTS idx_memories_task_active_importance ON proxy.memories(task_id, active, importance)"),
            ("idx_knowledge_active", "CREATE INDEX IF NOT EXISTS idx_knowledge_active ON proxy.knowledge(active)"),
            ("idx_working_memory_task", "CREATE INDEX IF NOT EXISTS idx_working_memory_task ON proxy.working_memory(task_id)"),
            ("idx_session_summaries_task", "CREATE INDEX IF NOT EXISTS idx_session_summaries_task ON proxy.session_summaries(task_id, created_at DESC)"),
            ("idx_phase_summaries_task", "CREATE INDEX IF NOT EXISTS idx_phase_summaries_task ON proxy.phase_summaries(task_id)"),
            ("idx_session_ledger_task", "CREATE INDEX IF NOT EXISTS idx_session_ledger_task ON proxy.session_ledger(task_id, status)"),
        ]
        for _idx_name, _idx_sql in _phase1_indexes:
            try:
                await _app.pool.execute(_idx_sql)
            except Exception as e:
                log.warning("Phase 1 index %s failed (non-fatal): %s", _idx_name, e)
        log.info("Phase 1 indexes applied")
    except Exception as e:
        log.warning("Phase 3 migration failed: %s", e)

    # Phase 2: Public readiness migrations
    try:
        _tenant_tables = [
            "proxy.tasks", "proxy.memories", "proxy.knowledge",
            "proxy.session_summaries", "proxy.phase_summaries",
            "proxy.session_ledger", "proxy.deliverables", "proxy.working_memory",
            "proxy.memory_jobs",
        ]
        for _tbl in _tenant_tables:
            await _app.pool.execute(f"ALTER TABLE {_tbl} ADD COLUMN IF NOT EXISTS tenant_id TEXT")
        await _app.pool.execute("""
            CREATE TABLE IF NOT EXISTS proxy.api_keys (
                hash TEXT PRIMARY KEY,
                label TEXT,
                disabled BOOLEAN DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await _app.pool.execute("DROP INDEX IF EXISTS proxy.knowledge_domain_key_active_idx")
        await _app.pool.execute("DROP INDEX IF EXISTS idx_knowledge_domain_key_active")
        await _app.pool.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_tenant_domain_key_active
            ON proxy.knowledge(tenant_id, domain, key) WHERE active = true
        """)
        for _idx_name, _idx_sql in [
            ("idx_memories_tenant_task", "CREATE INDEX IF NOT EXISTS idx_memories_tenant_task ON proxy.memories(tenant_id, task_id, active)"),
            ("idx_knowledge_tenant", "CREATE INDEX IF NOT EXISTS idx_knowledge_tenant ON proxy.knowledge(tenant_id, active)"),
            ("idx_session_summaries_tenant", "CREATE INDEX IF NOT EXISTS idx_session_summaries_tenant ON proxy.session_summaries(tenant_id, task_id)"),
            ("idx_phase_summaries_tenant", "CREATE INDEX IF NOT EXISTS idx_phase_summaries_tenant ON proxy.phase_summaries(tenant_id, task_id)"),
            ("idx_session_ledger_tenant", "CREATE INDEX IF NOT EXISTS idx_session_ledger_tenant ON proxy.session_ledger(tenant_id, task_id)"),
            ("idx_deliverables_tenant", "CREATE INDEX IF NOT EXISTS idx_deliverables_tenant ON proxy.deliverables(tenant_id)"),
            ("idx_working_memory_tenant", "CREATE INDEX IF NOT EXISTS idx_working_memory_tenant ON proxy.working_memory(tenant_id, task_id)"),
            ("idx_tasks_tenant", "CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON proxy.tasks(tenant_id)"),
        ]:
            try:
                await _app.pool.execute(_idx_sql)
            except Exception as e:
                log.warning("Phase 2 index %s failed (non-fatal): %s", _idx_name, e)
        log.info("Phase 2 migrations applied (tenant_id columns, api_keys table, knowledge index)")
    except Exception as e:
        log.warning("Phase 2 migration failed: %s", e)

    # Log LM data-flow policy
    if CTXGATE_LM_ENABLED:
        log.info("LM data-flow: conversation text WILL be sent to external LM at %s (model=%s)", MISTRAL_URL, MISTRAL_MODEL)
    else:
        log.info("LM data-flow: conversation text will NOT be sent to external LM (CTXGATE_LM_ENABLED=0)")

    # Tokenizer loading
    if ESTIMATOR_MODE == 'exact':
        if not QWEN_TOKENIZER_PATH:
            log.error("CTXGATE_QWEN_TOKENIZER is required. Set it to the path of tokenizer.json")
            raise SystemExit(78)
        if not os.path.isfile(QWEN_TOKENIZER_PATH):
            log.error("Tokenizer file not found: %s", QWEN_TOKENIZER_PATH)
            raise SystemExit(78)
    if ESTIMATOR_MODE == "exact":
        try:
            _app.enc = _tokenizers_mod.Tokenizer.from_file(QWEN_TOKENIZER_PATH)
            tok_name = "Qwen tokenizer.json"
            vocab = _app.enc.get_vocab_size()
            log.info("ctxgate-proxy started: tokenizer=%s vocab=%d, vllm=%s model=%s",
                     tok_name, vocab, VLLM_URL, VLLM_MODEL)
        except Exception as e:
            log.error("Failed to load tokenizer %s: %s (required for exact mode)", QWEN_TOKENIZER_PATH, e)
            raise SystemExit(78)
    else:
        log.info("ctxgate-proxy started: estimator=calibrated (rho=%.2f), vllm=%s model=%s",
                 RHO_COLD, VLLM_URL, VLLM_MODEL)

    # Restart recovery
    await _restart_recovery()

    # Eager crash recovery: prepopulate session baselines from Goose DB
    from proxy.hotpath.tracking import prepopulate_session_baselines
    prepopulate_session_baselines()

    # Start background loops
    worker_task = asyncio.create_task(_memory_worker_loop())
    hygiene_task = asyncio.create_task(_fd_hygiene_loop())
    _app._hygiene_task = hygiene_task

    # Create vLLM client
    _vllm_timeout = httpx.Timeout(
        _env_int("CTXGATE_VLLM_READ_TIMEOUT", 300),
        connect=_env_int("CTXGATE_VLLM_CONNECT_TIMEOUT", 10),
        write=_env_int("CTXGATE_VLLM_WRITE_TIMEOUT", 120),
        pool=_env_int("CTXGATE_VLLM_POOL_TIMEOUT", 30),
    )
    _app._vllm_client = httpx.AsyncClient(timeout=_vllm_timeout, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    log.info("shared vLLM httpx client created (timeout=%s)", _vllm_timeout)

    # Start Mistral priority queue + shared client + consumer
    _app._lm_queue = asyncio.PriorityQueue()
    _lm_timeout = httpx.Timeout(MISTRAL_TIMEOUT, connect=10)
    _app._lm_client = httpx.AsyncClient(timeout=_lm_timeout, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"} if MISTRAL_API_KEY else {})
    log.info("shared Mistral httpx client created (timeout=%ds, connect=10s, api_key=%s)", MISTRAL_TIMEOUT, "set" if MISTRAL_API_KEY else "MISSING")
    LM_WORKERS = int(os.environ.get("CTXGATE_LM_WORKERS", "10"))
    lm_consumer_tasks = [asyncio.create_task(_lm_consumer(i)) for i in range(LM_WORKERS)]
    log.info("Mistral %d independent workers started (rate limiter: %.1f RPS, %d TPM, model=%s)",
             LM_WORKERS, MistralRateLimiter.RPS_LIMIT, MistralRateLimiter.TPM_LIMIT, MISTRAL_MODEL)

    _sd_notify("READY=1")
    watchdog_task = asyncio.create_task(_watchdog_loop())
    log.info("systemd watchdog loop started (10s heartbeat, 60s timeout in unit)")

    # P4: Start periodic flush for window persists
    periodic_flush_task = asyncio.create_task(_periodic_flush_loop())
    
    yield

    # Shutdown
    # P4: Flush any pending window persists before shutdown
    await _flush_pending_persists()
    periodic_flush_task.cancel()
    worker_task.cancel()
    hygiene_task.cancel()
    watchdog_task.cancel()
    try:
        await worker_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await hygiene_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await watchdog_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await periodic_flush_task
    except (asyncio.CancelledError, Exception):
        pass
    for t in lm_consumer_tasks:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    if _app._lm_client is not None:
        await _app._lm_client.aclose()
        _app._lm_client = None
        _app._lm_queue = None
    if _app._vllm_client is not None:
        await _app._vllm_client.aclose()
        _app._vllm_client = None
    if _app.sqlite_conn:
        await _app.sqlite_conn.close()
    if _app.pool:
        await _app.pool.close()
    log.info("ctxgate-proxy shutdown complete (graceful: pool drained)")

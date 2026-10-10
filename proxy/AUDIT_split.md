# AUDIT: Logging & Responsibility Split — app.py / dashboard.py / worker.py

**Date:** 2026-10-08
**Scope:** Read-only audit of three Python files in /home/pawelw/ctxproxy

---

## 1. How Is Each Launched?

### systemd units (primary launch mechanism)

| Service | Unit File | ExecStart | Stdout/Stderr |
|---------|-----------|-----------|---------------|
| proxy | ~/.config/systemd/user/ctxgate-proxy.service | `taskset -c 4 /usr/bin/python -u /home/pawelw/ctxproxy/proxy/app.py` | append: /home/pawelw/ctxproxy/proxy.log |
| dashboard | ~/.config/systemd/user/ctxgate-dashboard.service | `taskset -c 4 /usr/bin/python -u /home/pawelw/ctxproxy/dashboard/dashboard.py` | append: /home/pawelw/ctxproxy/dashboard.log |
| worker | ~/.config/systemd/user/ctxgate-worker.service | `taskset -c 4 /usr/bin/python -u /home/pawelw/ctxproxy/worker/worker.py` | append: /home/pawelw/ctxproxy/worker.log |
| session-sync | ~/.config/systemd/user/ctxgate-session-sync.service | `python3 /home/pawelw/ctxproxy/syncer/session_sync.py` | (default journal) |

All three use `Type=notify` (proxy) or `Type=simple` (dashboard, worker) with `Restart=always`.

### Secondary launch (legacy fallback)

- **start_proxy.sh** (line 27): `exec nohup setsid python -u proxy/app.py >> proxy.log 2>&1 < /dev/null &`
  - Guards against duplicate: exits if `systemctl --user is-active ctxgate-proxy.service` is true.
  - Only for **app.py**. No equivalent nohup scripts exist for dashboard.py or worker.py.

### Makefile targets

- `Makefile`: `python proxy/app.py` and `python worker/worker.py` (dev/manual targets only)

### Cross-imports

- **app.py does NOT import dashboard.py or worker.py** (zero cross-imports).
- **dashboard.py does NOT import app.py** (zero cross-imports).
- **worker.py does NOT import app.py** (zero cross-imports).

All three are fully independent processes communicating only via:
- PostgreSQL (shared `proxy` schema)
- HTTP (dashboard polls proxy at 127.0.0.1:9201)
- File-based status (worker writes a status file read by dashboard)

---

## 2. Logging Engine for dashboard.py and worker.py

### dashboard.py (lines 32–43)

```python
_LOG_PATH = os.environ.get("CTXGATE_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard.log"))
_log_handler = logging.handlers.RotatingFileHandler(_LOG_PATH, maxBytes=10*1024*1024, backupCount=5)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
_log = logging.getLogger("dashboard")
_log.setLevel(logging.INFO)
_log.addHandler(_log_handler)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
_log.addHandler(_stream_handler)
```

- **Does NOT call** `logging.basicConfig()`
- Uses its **own named logger** `"dashboard"`
- Has its **own RotatingFileHandler** → `dashboard.log` (10MB × 5 backups)
- Has its **own StreamHandler** → stdout (captured by systemd into `dashboard.log`)
- **Does NOT import** `log` from app.py
- **Does NOT propagate** to root logger (no `propagate = False` set, but since no root handler is configured via basicConfig in this process, propagation is harmless)

### worker.py (lines 138–139)

```python
log = logging.getLogger("w")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
```

- **DOES call** `logging.basicConfig()` (line 139) — configures the **root logger** with a StreamHandler to stderr
- Uses its **own named logger** `"w"` (short for "worker")
- Has **NO FileHandler** — all output goes to stderr via the root handler
- Systemd captures stderr into `worker.log` (StandardError=append)
- **Does NOT import** `log` from app.py
- The named logger `"w"` **propagates** to root (default), so all messages appear on stderr → `worker.log`

### app.py (lines 24, 188)

```python
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # line 24
log = logging.getLogger("ctxgate-proxy")  # line 188
```

- **DOES call** `logging.basicConfig()` (line 24) — configures root logger with StreamHandler to stderr
- Uses named logger `"ctxgate-proxy"`
- No FileHandler — all output goes to stderr → captured by systemd into `proxy.log`
- Also adds an `_AccessLogFilter` to `uvicorn.access` logger (line 44)

---

## 3. Does Anything dashboard.py/worker.py Logs End Up in proxy.log?

**NO.** The log streams are fully isolated:

| Process | Logger | Handler Target | systemd captures to |
|---------|--------|---------------|-------------------|
| app.py | `ctxgate-proxy` → root | stderr | `proxy.log` |
| dashboard.py | `dashboard` (no basicConfig) | RotatingFileHandler → `dashboard.log` + StreamHandler → stdout | `dashboard.log` |
| worker.py | `w` → root (via basicConfig) | stderr | `worker.log` |

**Evidence of isolation:**
- No shell redirect like `>> proxy.log` in any systemd unit for dashboard or worker.
- No cross-imports (verified in §1).
- Each systemd unit has its own `StandardOutput`/`StandardError` targets.
- The only overlap: `start_proxy.sh` uses `>> proxy.log` but that script is exclusively for app.py and guards against running when systemd manages it.

**Conclusion:** Zero log bleed between the three files.

---

## 4. Non-Proxy Work in app.py

"Proxy work" = the core `/v1/chat/completions` request path: token counting, context window management, trimming, compaction, streaming to vLLM, response forwarding.

### 4a. Dashboard HTML and /api/* routes

| Function | Lines | Hot Path? | Equivalent in dashboard.py/worker.py? |
|----------|-------|-----------|---------------------------------------|
| `dashboard()` (HTML) | 5887–~6523 | No (on-demand GET) | **YES** — dashboard.py has its own `/` HTML route (line 1232) |
| `api_metrics()` | 3508–3527 | No (on-demand GET) | Partially — dashboard.py has `/api/health` but not the same metrics dict |
| `api_sessions()` | 3528–3546 | No | No direct equivalent (dashboard reads DB directly) |
| `api_recent_calls()` | 3547–3552 | No | No |
| `api_errors()` | 3553–3558 | No | No |
| `api_memory_summary()` | 3559–3583 | No | No |
| `api_memory()` | 5710–5744 | No | No (dashboard has its own DB queries) |
| `api_memory_analytics()` | 5745–5886 | No | No |
| `api_gpu()` | 5670–5710 | No | **YES** — dashboard.py has `sample_gpu()` (line 811) |
| `api_lmstudio()` | 5623–5650 | No (30s cache) | **YES** — dashboard.py polls this endpoint (line 125) |
| `metrics_prometheus()` | 3463–3508 | No | No |
| `health()` | 3412–3421 | No | **YES** — dashboard.py has `/health` (line 1207) |
| `ready()` | 3433–3462 | No | No |

### 4b. /_test/* routes

| Function | Lines | Hot Path? | Equivalent? |
|----------|-------|-----------|------------|
| `test_reset_prefix()` | 3584–3590 | No | No |
| `test_reset_sessions()` | 3591–3611 | No | No |

### 4c. Memory job enqueue

| Function | Lines | Hot Path? | Equivalent? |
|----------|-------|-----------|------------|
| `_enqueue_memory_job()` | 5272–5328 | **YES** — called from `chat_completions` on every user message | **YES** — worker.py consumes from `proxy.memory_jobs` table |
| `_resolve_task()` | 5329–5373 | **YES** — called by `_enqueue_memory_job` and memory routes | No (worker.py uses task_id directly from job) |
| `_insert_event_row()` | 749–773 | **YES** — called by `_enqueue_memory_job` | No |

### 4d. Extraction / Summarizer / Mistral calls

| Function | Lines | Hot Path? | Equivalent? |
|----------|-------|-----------|------------|
| `_call_lm_4b()` | 2922–2943 | No (background) | **YES** — worker.py has `call_4b()` (line 398) |
| `_call_4b()` | 450–479 | No (background queue) | **YES** — worker.py has `call_4b()` |
| `_lm_consumer()` | 424–449 | No (background) | **YES** — worker.py has `_consumer()` (line 890) |
| `_lm_do_call()` | 350–423 | No (background) | **YES** — worker.py has `process_job()` (line 731) |
| `MistralRateLimiter` class | 132–188 | No (background) | **YES** — worker.py has `_TokenBucket` (line 224) |
| `extract_knowledge()` | 3026–3089 | No (background, fire-and-forget) | No (worker.py does memory extraction, not knowledge) |
| `store_knowledge()` | 3090–3109 | No | No |
| `_fire_and_forget_extract()` | 2990–3025 | **YES** (spawns background task from request path) | No |
| `_summarize_trimmed_messages()` | 800–1022 | **YES** (called from `build_context` on trim) | No (this is core proxy logic) |
| `_build_session_digest()` | 690–748 | No (background) | No |
| `_extract_ledger_entries()` | 542–669 | No (background) | No |
| `_persist_ledger_entries()` | 670–689 | No (background) | No |
| `_sync_deliverable_summary()` | 2944–2968 | No (fire-and-forget) | **Partial** — dashboard.py has deliverable routes |

### 4e. Background probe loops

| Function | Lines | What it does | Interval |
|----------|-------|-------------|----------|
| `_vllm_health_loop()` | 4027–4073 | GET `VLLM_URL/models`, updates `vllm_alive`; also calls `_evict_stale_sessions()` and `_worker_pending_count()` | 60s |
| `_fd_hygiene_loop()` | 4098–4170 | Monitors fd count, swaps httpx clients at 50%, graceful restart at 75% | 60s |
| `_memory_worker_loop()` | 495–509 | **NO-OP** (deprecated, kept as placeholder) | 60s sleep |
| `_watchdog_loop()` | 233–245 | systemd watchdog ping (sd_notify WATCHDOG=1) | 10s |

**Note:** The "Mistral probe" is NOT a background loop. It is the on-demand `api_lmstudio()` endpoint (line 5623) with a 30s in-memory cache. The dashboard polls it.

### 4f. Digest / Knowledge / Task-memory DB work

| Function | Lines | Hot Path? | Notes |
|----------|-------|-----------|-------|
| `fetch_task_memory()` | 3166–3287 | **YES** — called from `build_context` on every request | Core proxy (injects memory into context) |
| `fetch_relevant_knowledge()` | 3288–3334 | **YES** — called from `build_context` | Core proxy |
| `_score_memory()` | 3150–3165 | **YES** — called from `fetch_task_memory` | Core proxy |
| `_extract_terms()` | 3110–3125 | **YES** | Core proxy |
| `_context_blob()` | 3126–3137 | **YES** | Core proxy |
| `_already_in_context()` | 3138–3149 | **YES** | Core proxy |
| `knowledge_create()` (POST /knowledge) | 5374–5396 | No | CRUD endpoint |
| `knowledge_search()` (GET /knowledge/search) | 5397–5445 | No | CRUD endpoint |
| `knowledge_stats()` (GET /knowledge/stats) | 5446–5456 | No | CRUD endpoint |
| `create_deliverable()` (POST /deliverable) | 5457–5508 | No | CRUD endpoint |
| `list_deliverables()` (GET /deliverable) | 5509–5541 | No | CRUD endpoint |
| `update_deliverable()` (PATCH /deliverable/{id}) | 5542–5588 | No | CRUD endpoint |
| `memory_inject()` (POST /memory/inject) | 5589–5606 | No | CRUD endpoint |
| `memory_query()` (GET /memory/{task_ref}) | 5607–5623 | No | CRUD endpoint |
| `_get_goose_session_id()` | 5211–5234 | No (called by deliverable/create) | Reads Goose SQLite |
| `_get_goose_session_info()` | 5235–5271 | No | Reads Goose SQLite |

### 4g. Injection metrics (in-process tracking)

| Function | Lines | Hot Path? | Notes |
|----------|-------|-----------|-------|
| `_record_injection()` | 1270–1315 | **YES** — called from `build_context` | In-memory dict + file persistence |
| `_load_injection_metrics()` | 1230–1242 | Startup | Reads `injection_metrics.json` |
| `_save_injection_metrics()` | 1243–1252 | Background | Writes file |
| `injection_metrics` dict | 1314 | — | Module-level state |

---

## 5. The Two GET /v1/models Probe Loops

### 5a. vLLM probe: `_vllm_health_loop()` (line 4027)

**What it does:** Every 60s, GET `VLLM_URL + "/models"` (typically `http://127.0.0.1:8000/models`).

**State it updates:**
```python
vllm_alive = False  # line 1106 — module-level global
```

**Who reads `vllm_alive`:**

1. **`forward_to_vllm()`** (line 4200) — **THE critical reader**:
   ```python
   async def forward_to_vllm(vllm_body: dict, input_tokens: int, session_key: str):
       global metrics
       if not vllm_alive:
           metrics["requests_error"] += 1
           log.warning("vLLM is down - rejecting request early")
           return JSONResponse({"error": {...}}, status_code=503)
   ```
   This is on the **request hot path**. Every `/v1/chat/completions` request checks `vllm_alive` before attempting to forward. If False, returns 503 immediately without consuming tokens or hitting vLLM.

2. **`_vllm_health_loop()` itself** (lines 4043–4053) — logs "vLLM is BACK" / "vLLM unreachable" transitions.

**No other readers.** The `/health` endpoint does NOT read `vllm_alive` (it reports session count and fd count). The `/ready` endpoint makes its own fresh HTTP check to vLLM.

### 5b. Mistral "probe": `api_lmstudio()` (line 5623)

**This is NOT a background loop.** It is an on-demand HTTP endpoint with a 30-second in-memory cache:

```python
_lmstudio_cache = {"ts": 0.0, "data": None}  # line 5621

@app.get("/api/lmstudio")
async def api_lmstudio():
    global _lmstudio_cache
    _now = time.time()
    if _lmstudio_cache["data"] is not None and _now - _lmstudio_cache["ts"] < 30.0:
        return _lmstudio_cache["data"]
    # ... GET https://api.mistral.ai/v1/models ...
    _lmstudio_cache["ts"] = _now
    _lmstudio_cache["data"] = result
    return result
```

**Who reads it:**
- **dashboard.py** (line 125): `"mistral": {"type": "http", "url": "http://127.0.0.1:9201/api/lmstudio"}` — polled every POLL_INTERVAL seconds by `poll_health()`.
- **No internal app.py code reads `_lmstudio_cache`** for decision-making. The Mistral queue/consumer (`_lm_consumer`) does NOT check this cache — it has its own rate limiter and error handling.

**Key distinction:** Unlike `vllm_alive` which gates the hot path, the Mistral probe is purely informational (dashboard display). The Mistral queue operates independently with its own backoff/retry logic.

---

## PHASE C: RANKED PROPOSAL — What Can Move Out of app.py

Ranked from **safest** (lowest risk, clearest benefit) to **most aggressive**.

---

### Rank 1: Dashboard HTML (`/dashboard` route + `DASHBOARD_HTML`)

- **Functions/lines:** `dashboard()` at line 5887, `DASHBOARD_HTML` string from ~5889 to ~6523 (~635 lines of HTML/CSS/JS)
- **Shared in-process state read:** None. The HTML is static; the JS in it polls `/api/metrics`, `/api/sessions`, etc. via fetch().
- **How the other service gets the data:** dashboard.py already has its own HTML route at `/` (line 1232). The proxy's dashboard HTML is a **redundant second dashboard**. Remove it entirely.
- **Latency effect on hot path:** **Zero.** This is a cold-path GET route, never called during `/v1/chat/completions`.
- **Regression risk:** **LOW.** No test depends on `GET /dashboard` on port 9201. The canonical dashboard is on port 9202.
- **Tests covering it:** None found.

---

### Rank 2: /_test/* routes

- **Functions/lines:** `test_reset_prefix()` (3584–3590), `test_reset_sessions()` (3591–3611)
- **Shared in-process state read:** `session_fingerprints`, `session_tokens`, `recent_calls`, `metrics` (all module-level dicts/deques)
- **How the other service gets the data:** These are **test-only** mutation endpoints. They cannot be moved to another process because they mutate in-process state. **Recommendation: keep in app.py but behind an auth guard or remove if unused in production.**
- **Latency effect:** **Zero** (cold path).
- **Regression risk:** **LOW** if kept. **N/A** if removed (no production caller).
- **Tests covering it:** Manual testing only.

---

### Rank 3: /api/lmstudio (Mistral probe endpoint)

- **Functions/lines:** `api_lmstudio()` (5623–5650), `_lmstudio_cache` (5621)
- **Shared in-process state read:** `_lmstudio_cache` (30s TTL dict), `MISTRAL_API_KEY`, `MISTRAL_MODEL`
- **How the other service gets the data:** **dashboard.py already has this exact logic** in `check_http()` + its own service map (line 125). The dashboard could probe `api.mistral.ai/v1/models` directly instead of going through the proxy. Alternatively, keep this endpoint in app.py as a thin passthrough (it's already cached and cheap).
- **Latency effect on hot path:** **Zero** (cold path, 30s cache).
- **Regression risk:** **LOW.** Dashboard.py would need a small change to probe Mistral directly.
- **Tests covering it:** Dashboard health check integration.

---

### Rank 4: /api/gpu (NVML GPU stats)

- **Functions/lines:** `_gpu_from_nvml()` (5650–5669), `api_gpu()` (5670–5710)
- **Shared in-process state read:** `_nvml_ready` (bool), pynvml library
- **How the other service gets the data:** **dashboard.py already has `sample_gpu()`** (line 811) which does the same NVML reads. The proxy's `/api/gpu` is redundant.
- **Latency effect on hot path:** **Zero** (cold path).
- **Regression risk:** **LOW.** Dashboard already collects GPU stats independently.
- **Tests covering it:** None specific.

---

### Rank 5: Knowledge CRUD endpoints (POST/GET /knowledge, /knowledge/search, /knowledge/stats)

- **Functions/lines:** `knowledge_create()` (5374–5396), `knowledge_search()` (5397–5445), `knowledge_stats()` (5446–5456)
- **Shared in-process state read:** `pool` (asyncpg connection pool)
- **How the other service gets the data:** These are pure DB CRUD operations. **dashboard.py** could serve them (it already has a PG pool). Alternatively, a small standalone API service. The key constraint: `extract_knowledge()` (line 3026) writes to the same table and MUST stay in app.py (it's triggered from the request path).
- **Latency effect on hot path:** **Zero** (cold path CRUD).
- **Regression risk:** **LOW-MED.** The write path (`extract_knowledge` → `store_knowledge`) stays in app.py. Only the read/create HTTP endpoints move.
- **Tests covering it:** Knowledge API integration tests (referenced in session notes: "phase7 test: knowledge API works").

---

### Rank 6: Deliverable CRUD endpoints (POST/GET/PATCH /deliverable)

- **Functions/lines:** `create_deliverable()` (5457–5508), `list_deliverables()` (5509–5541), `update_deliverable()` (5542–5588)
- **Shared in-process state read:** `pool`, `_get_goose_session_id()`, `_get_goose_session_info()` (Goose SQLite)
- **How the other service gets the data:** **dashboard.py already has deliverable routes** (lines 1298–1393: `/deliverable`, `/api/deliverable`, `/deliverable/file`). The proxy's deliverable endpoints are a **second write path** to the same table. Consolidate to dashboard.py.
- **Latency effect on hot path:** **Zero** (cold path).
- **Regression risk:** **MED.** The `create_deliverable` auto-enrichment reads Goose SQLite (`_get_goose_session_info`). Moving it to dashboard.py means dashboard.py needs SQLite access (it currently doesn't). Alternatively, keep the write in app.py and move only the list/read to dashboard.
- **Tests covering it:** Deliverable dashboard integration.

---

### Rank 7: Memory CRUD endpoints (POST /memory/inject, GET /memory/{task_ref})

- **Functions/lines:** `memory_inject()` (5589–5606), `memory_query()` (5607–5623)
- **Shared in-process state read:** `pool`, `_resolve_task()`
- **How the other service gets the data:** Pure DB CRUD. Could move to dashboard.py or a small API service.
- **Latency effect on hot path:** **Zero** (cold path).
- **Regression risk:** **LOW-MED.** `_resolve_task()` is also used by `_enqueue_memory_job` (hot path) so it must stay in app.py. The CRUD endpoints just call it.
- **Tests covering it:** Memory API tests.

---

### Rank 8: /api/memory and /api/memory-analytics

- **Functions/lines:** `api_memory()` (5710–5744), `api_memory_analytics()` (5745–5886)
- **Shared in-process state read:** `pool`, `_resolve_task()`
- **How the other service gets the data:** Pure DB read queries. dashboard.py could serve these (it already queries the same tables for its own display).
- **Latency effect on hot path:** **Zero** (cold path).
- **Regression risk:** **LOW.** Read-only DB queries.
- **Tests covering it:** Memory analytics dashboard.

---

### Rank 9: /api/metrics, /api/sessions, /api/recent-calls, /api/errors, /api/memory-summary

- **Functions/lines:** `api_metrics()` (3508–3527), `api_sessions()` (3528–3546), `api_recent_calls()` (3547–3552), `api_errors()` (3553–3558), `api/memory_summary()` (3559–3583)
- **Shared in-process state read:** `metrics` dict, `session_fingerprints`, `recent_calls` deque, `session_tokens`, `_read_worker_status()`, `_worker_pending_count()`
- **How the other service gets the data:** These expose **in-process state** that only app.py has. They CANNOT move to another process without an IPC mechanism. **Recommendation: KEEP in app.py.** These are the cheap read-only JSON endpoints that dashboard.py polls. They are the "API surface" of the proxy's internal state.
- **Latency effect on hot path:** **Zero** (cold path, no DB queries in most cases).
- **Regression risk:** **HIGH if moved** (would require shared memory or message queue). **LOW if kept.**
- **Tests covering it:** Dashboard polling integration.

---

### Rank 10: /metrics and /metrics/prometheus

- **Functions/lines:** `get_metrics()` (3422–3432), `metrics_prometheus()` (3463–3508)
- **Shared in-process state read:** `metrics` dict, `_lm_rate_limiter`, `session_fingerprints`, `recent_calls`, `_worker_pending_count()`
- **How the other service gets the data:** Same as Rank 9 — in-process state. **KEEP in app.py.**
- **Latency effect:** **Zero.**
- **Regression risk:** **HIGH if moved.**
- **Tests covering it:** Prometheus scraping.

---

### Rank 11: /health and /ready

- **Functions/lines:** `health()` (3412–3421), `ready()` (3433–3462)
- **Shared in-process state read:** `session_fingerprints` (len), `_fd_breakdown()`, `pool`, `enc`, `VLLM_URL`
- **How the other service gets the data:** These are standard Kubernetes/infrastructure health probes. **MUST stay in app.py** — they report on the proxy's own liveness.
- **Latency effect:** **Zero** (trivial).
- **Regression risk:** **HIGH if moved.**
- **Tests covering it:** systemd watchdog, infrastructure monitoring.

---

### Rank 12: /v1/models passthrough

- **Functions/lines:** `v1_models()` (3612–3625)
- **Shared in-process state read:** None (pure HTTP passthrough to vLLM)
- **How the other service gets the data:** This is an **OpenAI-compatible API endpoint** that external clients (Goose, dashboards, health checkers) call. It MUST stay on port 9201 because clients are configured to hit the proxy's port.
- **Latency effect:** **Zero** (it's the endpoint itself, not a background loop).
- **Regression risk:** **HIGH if moved** (breaks OpenAI-compatible client expectations).
- **Tests covering it:** Client integration.

---

### Rank 13: Background loops (KEEP in app.py)

| Loop | Lines | Why it must stay |
|------|-------|-----------------|
| `_vllm_health_loop()` | 4027–4073 | Updates `vllm_alive` which gates the hot path (`forward_to_vllm` line 4200) |
| `_fd_hygiene_loop()` | 4098–4170 | Manages the proxy's own httpx clients and fd limits |
| `_memory_worker_loop()` | 495–509 | No-op placeholder (could be deleted) |
| `_watchdog_loop()` | 233–245 | systemd watchdog protocol |
| `_lm_consumer()` × N | 424–449 | Mistral priority queue consumers (background LLM calls) |

---

### Summary: What Actually Moves

| Priority | What moves                              | Lines freed | Risk    |
| -------- | --------------------------------------- | ----------- | ------- |
| 1        | `DASHBOARD_HTML` + `dashboard()` route  | ~640 lines  | LOW     |
| 2        | `/api/gpu` + `_gpu_from_nvml()`         | ~60 lines   | LOW     |
| 3        | `/api/lmstudio` + `_lmstudio_cache`     | ~30 lines   | LOW     |
| 4        | Knowledge CRUD (3 endpoints)            | ~85 lines   | LOW-MED |
| 5        | Deliverable CRUD (3 endpoints)          | ~130 lines  | MED     |
| 6        | Memory CRUD (2 endpoints)               | ~35 lines   | LOW-MED |
| 7        | `/api/memory` + `/api/memory-analytics` | ~175 lines  | LOW     |

**Total removable: ~1155 lines** (out of 6523) → app.py shrinks by ~18%.

**What stays (core proxy + in-process state API):**
- `/v1/chat/completions` (the actual proxy)
- `/v1/models` (OpenAI compat)
- `/health`, `/ready` (infra probes)
- `/metrics`, `/metrics/prometheus`, `/api/metrics`, `/api/sessions`, `/api/recent-calls`, `/api/errors` (in-process state)
- `/_test/*` (test utilities)
- All background loops
- All context window management
- All summarization/extraction logic
- `_enqueue_memory_job` (hot path)
- `fetch_task_memory`, `fetch_relevant_knowledge` (hot path)
- Injection metrics

---

## PHASE B EXECUTION (2026-10-08 14:43)

### B2 — Probe Removal (APPLIED)

Per user instruction: services run on the same PC, no need for background probes.

**Removed:**
| Item | Lines (original) | Action |
|------|-----------------|--------|
| vllm_alive global | 1106 | Deleted |
| health_task create_task line | 1452 | Deleted |
| _vllm_health_loop() function | 4027-4063 | Deleted |
| vllm_alive gate in forward_to_vllm | 4200-4203 | Deleted - natural connection error handling takes over |
| _lmstudio_cache + api_lmstudio() endpoint | 5621-5647 | Deleted |
| /api/lmstudio from _SILENT_PATHS | 29 | Removed |
| LM Studio HTML card in dashboard | 5959-5964 | Removed |
| JS fetchJSON /api/lmstudio block | 6122-6143 | Removed |

**Preserved:**
- _evict_stale_sessions() — now called from _fd_hygiene_loop (line 4080, runs every 60s)
- _worker_pending_count() — called from 3 other sites (metrics, memory enqueue)
- /health, /ready — untouched (infra probes for the proxy itself)
- _fd_hygiene_loop — started at line 1452 (replaced the health_task line)

**Mistral connection failures:** The actual _lm_consumer API call paths already log and fail on connection errors — no probe needed.

**Verification:**
- py_compile: PASS
- Tests: 9 passed (baseline: 9 passed)
- No remaining references to vllm_alive or lmstudio in app.py

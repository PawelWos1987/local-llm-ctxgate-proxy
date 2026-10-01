# local-llm-ctxgate-proxy Architecture

A lightweight local context gate proxy for long-running AI coding agents.

local-llm-ctxgate-proxy (Python/FastAPI, ~1390 LOC) sits between Goose (the AI agent) and vLLM (the LLM inference server). It enforces context-window boundaries, supplements context with conditional memory injection, and streams responses — all with < 5 ms added latency on the hot path.

---

## 1. Three-Layer Separation

The system is built on three distinct layers, each responsible for a different timescale of memory:

| Layer | Role | Timescale |
|---|---|---|
| **Goose compaction** | Short-term continuity — in-context summarization | Per-session, triggered at 90% of window (backstop) |
| **ctxgate-proxy** | Context boundary + small conditional memory supplement | Per-request, deterministic |
| **4B memory worker** | Async durable-memory extraction | Per-turn, background |

### 1.1 Goose Compaction (Safety Gate — Last Resort)

Goose's in-context summarization is a **safety gate**, not the primary context manager. It is enabled at `GOOSE_AUTO_COMPACT_THRESHOLD = 0.90`. On an 84,000-token window this triggers at **75,600 total tokens** — a hard ceiling that only fires if ctxgate-proxy's rolling-window trim is unable to keep the request within bounds.

**Division of labor:**

| Mechanism | Trigger | Role |
|---|---|---|
| **ctxgate-proxy** (primary) | Every request — trims input to `MAX_INPUT = 64,000` | Deterministic context boundary, the workhorse |
| **Goose auto-compact** (safety gate) | Context reaches `0.90 × 84,000 = 75,600` | Last-resort summarization if the proxy's rolling window is insufficient |

Why the proxy is primary and Goose compact is the fallback:
- **The agent sees the full 84k window** and grows context freely — it does not self-limit.
- **The proxy trims what is forwarded to vLLM** on every request: 64k input + 18k output + 2k margin = 84k. Deterministic, lossy middle-message trimming at < 5 ms.
- **Goose compact at 0.90** is a coarser, lossy summarization that only runs if the proxy cannot keep the window in check — a backstop, not the steady-state path.
- **The 4B memory worker** supplies the durable facts that let the agent recover what the proxy trimmed, so the safety gate rarely needs to fire.

Goose compaction is the **index** — when it does run, it tells the agent which files and sessions to re-read. It keeps a single session oriented.

### 1.2 ctxgate-proxy (Context Boundary + Conditional Memory)

The proxy is a **deterministic gate** on every request:

1. **Token-aware assembly**: counts tokens using the Qwen tokenizer (not byte-estimates). Assembles the prompt from system + tools + messages, trimming oldest middle messages first when input exceeds MAX_INPUT (64,000 tokens).
2. **Conditional memory injection**: before sending to vLLM, checks if relevant memories exist in PostgreSQL that are NOT already in the current context. Uses a 60% overlap dedup gate — if the fact is already visible to the model, it is NOT re-injected. This keeps the hot path at < 5 ms when context is sufficient.
3. **SSE streaming passthrough**: responses stream directly from vLLM to Goose with no buffering.
4. **Session isolation**: each Goose session gets its own task_id. Memories are scoped per-task. Cross-session knowledge sharing is opt-in via the knowledge table.

The proxy **never blocks** on the 4B model. Memory extraction is fully async.

### 1.3 4B Memory Worker (Async Durable Memory)

A separate Python process (systemd user service) polls proxy.memory_jobs and calls the 4B LM Studio model to extract structured memories from conversation events.
**Single-instance by design**: the worker enforces a single running instance via a `flock` on `worker/.worker.lock` (kernel-released on process death), a heartbeat written each poll cycle (`pid=` + `heartbeat=` epoch), and a stale-kill/takeover check on startup (a holder whose heartbeat is older than `CTXGATE_WORKER_LOCK_TTL`, default 30 s, is SIGKILLed; a healthy holder causes the new instance to exit). The systemd unit (`ctxproxy-worker.service`) also sets `WatchdogSec=30` so systemd independently kills a worker that stops pinging. Only one unit runs the worker.

**Model**: qwen3-4b-instruct-2507 (higher quantization, LM Studio :1234)

**Structured Output Schema** (enforced via response_format.type = json_schema):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["memory_actions", "state_update"],
  "properties": {
    "memory_actions": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["action", "type", "importance", "title", "content", "source_event_id"],
        "properties": {
          "action": { "type": "string", "enum": ["NEW", "UPDATE", "SUPERSEDE", "DUPLICATE", "NO_CHANGE"] },
          "type": { "type": "string", "enum": ["DECISION", "FINDING", "FAILURE", "TODO", "CONSTRAINT", "FILE", "STATE", "FACT"] },
          "importance": { "type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"] },
          "title": { "type": "string" },
          "content": { "type": "string" },
          "source_event_id": { "type": "string" }
        }
      }
    },
    "state_update": {
      "type": "object",
      "additionalProperties": false,
      "required": ["changed", "current_state", "current_subtask"],
      "properties": {
        "changed": { "type": "boolean" },
        "current_state": { "type": ["string", "null"] },
        "current_subtask": { "type": ["string", "null"] }
      }
    }
  }
}
```

**Field mapping** (schema → DB columns):

| Schema field | DB column | Notes |
|---|---|---|
| title | key | Normalized for dedup (lowercase, alnum-only) |
| content | value | Free-text |
| type | category | Free-text (DECISION, FINDING, etc.) |
| importance | importance | String→int map: CRITICAL=10, HIGH=7, NORMAL=5, LOW=2 |
| source_event_id | source_event_id | UUID, traceability |
| — | model_name | Auto-populated: qwen3-4b-instruct-2507 |

**Dedup & lifecycle**:
- **NEW**: INSERT if no matching key (normalized) exists for the task
- **UPDATE**: UPDATE value/importance on existing key
- **SUPERSEDE**: mark old row active=false, set superseded_by to new row's ID, INSERT new
- **DUPLICATE / NO_CHANGE**: no-op, job marked done with applied=0

**Outage handling**: if LM Studio is unreachable, jobs stay pending with exponential backoff (2s → 60s cap) for up to CTXGATE_WORKER_OUTAGE_TTL (default 1800s = 30 min). Only after that window expires are jobs marked failed. The attempts column tracks retry count.

**Pre-load**: on startup, the worker checks GET /v1/models. If the model is absent, it triggers POST /v1/models/load and polls for up to 90s. This prevents the 120s request timeout from being consumed by model load time.

**No loops**: the 4B's output NEVER enqueues a new memory job. Only original Goose/user/tool events (enqueued by proxy/app.py) create jobs.

---

## 2. Data Flow

```
Goose Agent
    |
    |  POST /v1/chat/completions  (http://127.0.0.1:9200)
    v
+-------------------------------------------+
|  ctxgate-proxy (FastAPI, :9200)           |
|                                           |
|  1. Token-count input (Qwen tokenizer)   |
|  2. Trim to MAX_INPUT=64000 if needed    |
|  3. Conditional memory injection         |
|     (dedup 60% overlap + relevance gate) |
|  4. Forward to vLLM                      |
|  5. Stream SSE response back to Goose    |
|  6. Enqueue memory job (async, non-block)|
+------------------+------------------------+
                   |
                   |  http://127.0.0.1:29000/v1
                   v
              vLLM (Qwen3.8-27B, 84k window)

                   |  (async, separate process)
                   v
+-------------------------------------------+
|  4B Memory Worker (systemd user service) |
|                                           |
|  1. Poll memory_jobs (FOR UPDATE SKIP    |
|     LOCKED, parallelism=1)               |
|  2. Call qwen3-4b-instruct-2507          |
|     (LM Studio :1234, json_schema)      |
|  3. Validate + dedup + apply to PG      |
|  4. Update working_memory               |
|  5. Mark job done/failed                |
+-------------------------------------------+
                   |
                   v
            PostgreSQL (ctxproxy DB)
            - proxy.tasks
            - proxy.events
            - proxy.memories  (model_name column)
            - proxy.working_memory
            - proxy.memory_jobs  (attempts column)
            - proxy.knowledge
```

---

## 3. Key Design Decisions

| Decision | Rationale |
|---|---|
| **Conditional injection** (not blanket) | Zero hot-path overhead when context is already sufficient. 60% overlap gate + relevance check. |
| **Async 4B worker** (not inline) | 27B never waits for 4B. A bad 4B response never blocks the agent. |
| **json_schema response_format** | LM Studio 4B only accepts json_schema (not json_object). Strict schema prevents malformed output. |
| **additionalProperties: false** | 4B models hallucinate extra fields. This kills that failure mode at the API level. |
| **Token-aware trimming** (not char-based) | Qwen tokenizer gives accurate counts. Char-based estimates are 20-30% off. |
| **Per-task memory scoping** | Sessions don't pollute each other. Cross-session sharing is opt-in via knowledge table. |
| **Outage TTL + backoff** | Prevents burning 3 attempts in 6s during a 30-min LM Studio outage. Jobs stay pending, retry with exponential backoff. |
| **model_name column** | Tracks which 4B model produced each memory. Essential when switching quantizations. |

---

## 4. Security Boundary

- **Localhost only**: both the proxy (:9200) and LM Studio (:1234) bind to 127.0.0.1. No external exposure.
- **Single-user**: designed for one developer on one machine. No authentication, no multi-tenancy.
- **PostgreSQL**: local instance, password-protected. The DSN is in a systemd user service file (0600) and .env (gitignored).
- **No PII in memories**: the 4B extracts technical facts (config values, decisions, file paths). The prompt explicitly instructs it to ignore personal data.
- **See SECURITY.md** for the full threat model and vulnerability reporting process.

---

## 5. File Layout

```
local-llm-ctxgate-proxy/
├── proxy/
│   └── app.py              # FastAPI proxy (~1390 LOC)
├── worker/
│   └── worker.py           # 4B memory worker (~480 LOC)
├── schema/
│   ├── 001_init.sql        # Core tables (tasks, events, memories, working_memory)
│   ├── 002_knowledge.sql   # Cross-session knowledge sharing
│   └── 003_memory_worker.sql  # memory_jobs + attempts + model_name
├── tests/
│   ├── test_architecture.py  # 11-check architecture suite (21 test files total)
│   ├── test_e2e_4b_memory.py # End-to-end 4B pipeline
│   ├── test_perf_abc.py      # Performance benchmarks
│   └── test_8m_longrun.py    # 8M-token long-run stress test
├── .github/
│   ├── workflows/
│   │   ├── ci.yml          # Python 3.11/3.12 matrix + PG16
│   │   └── codeql.yml      # SAST (push/PR + weekly)
│   └── dependabot.yml      # pip + github-actions weekly
├── .env.example
├── ARCHITECTURE.md
├── README.md
├── SECURITY.md
├── LICENSE
└── requirements.txt
```

---

## 6. Environment Variables

| Variable | Default | Description |
|---|---|---|
| CTXGATE_DB_DSN | postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy | PostgreSQL connection string |
| CTXGATE_VLLM_URL | http://127.0.0.1:29000/v1 | vLLM server URL |
| CTXGATE_VLLM_MODEL | Qwen3.8-27B | Model name served by vLLM |
| CTXGATE_QWEN_TOKENIZER | /path/to/tokenizer.json | Qwen tokenizer for accurate counting |
| CTXGATE_LM_URL | http://127.0.0.1:1234/v1/chat/completions | LM Studio 4B endpoint |
| CTXGATE_LM_MODEL | qwen3-4b-instruct-2507 | 4B model name |
| CTXGATE_WORKER_POLL | 2.0 | Worker poll interval (seconds) |
| CTXGATE_WORKER_MAX_ATTEMPTS | 3 | Max retries per job (non-outage) |
| CTXGATE_WORKER_OUTAGE_TTL | 1800 | Outage backoff window (seconds) |
| CTXGATE_WORKER_MAX_TOKENS | 512 | Max output tokens for 4B |
| CTXGATE_MAX_CONTEXT | 84000 | Total context window (tokens) |
| CTXGATE_MAX_INPUT | 64000 | Max input tokens before trimming |
| CTXGATE_MAX_OUTPUT | 18000 | Max output tokens |
| CTXGATE_SAFETY_MARGIN | 2000 | Token safety margin |
| CTXGATE_PROXY_PORT | 9200 | Proxy listen port |
| CTXGATE_API_KEY | (off) | Optional bearer auth on /v1/chat/completions |
| CTXGATE_MAX_BODY_BYTES | 20971520 | Max request body size (20 MB) |
| CTXGATE_WORKER_CONCURRENCY | 1 | Worker parallelism |
| CTXGATE_WORKER_LOCK | worker/.worker.lock | Lock file path for single-instance guard |
| CTXGATE_WORKER_LOCK_TTL | 30 | Staleness threshold for frozen-holder detection (seconds) |


---

## 7. Shutdown & Graceful Stop

Both processes receive SIGTERM/SIGINT on stop. Their behavior differs — the worker is explicitly graceful, the proxy relies on uvicorn defaults.

### 7.1 4B Memory Worker (worker/worker.py)
- Explicit signal handlers (_sig) for SIGTERM and SIGINT set a running = False flag.
- The poll() loop is while running: — on signal it lets the **in-flight process_job() finish**, then exits. Its finally block closes the httpx client and the asyncpg pool.
- **Pending jobs are NOT lost**: unclaimed jobs stay in proxy.memory_jobs with status='pending' and are re-claimed on the next start. A job in outage-backoff requeues as pending.
- SIGKILL (no signal) aborts immediately; the current job is left processing and is re-claimed/retried on restart (bounded by attempts / MAX_ATTEMPTS).

### 7.2 Proxy (proxy/app.py)
- Uses the FastAPI lifespan context manager: on startup it creates the asyncpg pool; on shutdown it runs await pool.close() and logs "ctxgate-proxy shutdown complete (graceful: pool drained)".
- uvicorn.run(app, ...) installs uvicorn's built-in SIGTERM/SIGINT handling, which triggers that lifespan shutdown.
- **In-flight SSE streams**: uvicorn's default timeout_graceful_shutdown=None means it stops accepting *new* connections but **waits for in-flight streams to complete** before exiting. A stream is therefore not torn down by a plain SIGTERM — but there is no *upper bound*, so a hung stream can block shutdown indefinitely.
- **SIGKILL** bypasses all of this: the process dies instantly and any open stream is cut.

### 7.3 Graceful Shutdown (IMPLEMENTED)
- **Bound the proxy's graceful window** so a hung stream cannot block shutdown forever: `timeout_graceful_shutdown=30` is set on `uvicorn.run()`. In-flight streams get up to 30 s to finish before uvicorn force-closes them. The pool is drained by the lifespan.
- The worker already has the correct pattern (finish current job, persist the rest) and needs no change.
- **Operational rule**: always stop with SIGTERM (kill <pid>), never SIGKILL, so streams drain and the pool closes cleanly.


---

## 8. Memory TTL / Expiration (IMPLEMENTED)

`proxy.memories` has `last_accessed_at` and `expires_at` columns (added by `schema/004_memory_ttl.sql`). The 4B worker **supersedes** a memory when a fact changes (sets `active=false, status='superseded'`). A periodic prune job in the worker (`prune_memories()`, 6-hour cadence) hard-expires rows past `expires_at` and age-prunes non-critical rows not accessed within `CTXGATE_MEMORY_TTL_DAYS` (default 90). CRITICAL (importance=10) rows are never pruned.

The `last_accessed_at` column is stamped on every injection in `fetch_task_memory()` (touch-on-use), so actively-used memories are protected from pruning.

**Implementation:**
- `last_accessed_at TIMESTAMPTZ` — touched each time a memory is **injected** into the main model in `fetch_task_memory` (touch-on-use `UPDATE`). `expires_at TIMESTAMPTZ` (nullable; set per-category).
- Touch on use: `fetch_task_memory` runs `UPDATE proxy.memories SET last_accessed_at=now() WHERE id=ANY($ids)` after selecting rows — "used" memories are protected from pruning.
- Periodic prune job in the worker (`prune_memories()`, 6-hour cadence, not the hot path):
  - Hard expire: `DELETE FROM proxy.memories WHERE expires_at IS NOT NULL AND expires_at < now()`
  - Age prune of never-used: `DELETE FROM proxy.memories WHERE active AND last_accessed_at IS NOT NULL AND last_accessed_at < now() - interval '90 days'`
  - Keep it conservative: never prune `importance=10` (CRITICAL) or recently-superseded rows; log every prune.
- Retention window: `CTXGATE_MEMORY_TTL_DAYS` env var (default 90), tunable per deployment.

Monitor via the dashboard's Produced Memories panel. The prune job runs automatically; manual pruning is rarely needed.

---

## 9. Configuration Validation at Startup

### Previous State: Log-Only (No Fail-Fast) — Now Replaced

```python
def _validate_config(tok_name: str) -> None:
    try:
        from urllib.parse import urlparse
        u = urlparse(DB_DSN)
        log.info("config: dsn_host=%s db=%s vllm=%s ...", ...)
        if MAX_INPUT + SAFETY_MARGIN > MAX_CONTEXT:
            log.warning("config: MAX_INPUT(%d)+SAFETY_MARGIN(%d) > MAX_CONTEXT(%d)", ...)
    except Exception as e:
        log.warning("config validation: %s", e)
```

The old `_validate_config()` was log-only and ran after pool creation. It has been **replaced** by `validate_config()` which runs **before** `asyncpg.create_pool()` and calls `sys.exit(1)` on any failure.

### The Import-Time Crash Problem

Numeric environment variables are parsed with bare `int()` at **module import time**:

```python
# proxy/app.py (top of file)
MAX_CONTEXT = int(os.environ.get("CTXGATE_MAX_CONTEXT", "8192"))
MAX_INPUT   = int(os.environ.get("CTXGATE_MAX_INPUT",   "6144"))
# ...
```

If an operator sets `CTXGATE_WORKER_OUTAGE_TTL=abc`, the process dies at import with a raw `ValueError: invalid literal for int() with base 10: 'abc'` — no context, no list of other problems, no graceful message.

### Implementation

1. **`_env_int(name, default)` helper** (line 37) — returns `int(os.environ[name])` on success, logs a warning and returns `default` on `ValueError`. All 7 numeric env vars use it. Eliminates the import-time crash.
2. **`validate_config()` in the FastAPI `lifespan`** — called **before** `asyncpg.create_pool()`. Collects all problems, then `sys.exit(1)`. Checks:
   - DSN parses and has a reachable host (optional TCP probe)
   - `VLLM_URL` is a valid HTTP/HTTPS URL
   - `MAX_INPUT + SAFETY_MARGIN <= MAX_CONTEXT`
   - `MAX_OUTPUT >= 1`
   - Tokenizer file exists (if Qwen path is set)
   - Collects **all** problems into a list, logs each, then `sys.exit(1)` if any are found.

Operators get a single, complete error report at startup instead of a cryptic crash or a silent misconfiguration.

---

## 10. Token Budget Enforcement for Injected Memory

### Budget Model

`fetch_task_memory()` enforces a **hard cap of 2 000 tokens** total, split into two tiers:

| Tier | Budget | Source |
|------|--------|--------|
| Working memory | 800 tokens | `proxy.working_memory` (1 row) |
| Durable memories | 1 200 tokens | `proxy.memories` (up to 13 rows) |
| **Total** | **2 000 tokens** | — |

### Row Selection (SQL-level caps)

```sql
-- CRITICAL: at most 5 rows
SELECT key, value FROM proxy.memories
WHERE task_id=$1 AND active=true AND importance=10
ORDER BY updated_at DESC LIMIT 5;

-- Relevant: at most 8 rows
SELECT key, value FROM proxy.memories
WHERE task_id=$1 AND active=true
  AND (key ILIKE ANY($2) OR value ILIKE ANY($2))
ORDER BY importance DESC, updated_at DESC LIMIT 8;
```

5 + 8 = **13 rows maximum** are ever considered.

### Injection Algorithm (no mid-row truncation)

1. Deduplicate rows (normalized lower-case key+value).
2. Skip rows already present in the current Goose context (`_already_in_context`).
3. Skip rows whose value is a substring of the working-memory text.
4. For each remaining row, compute `count_tokens(line)`.
5. **Hard break**: if `total + t > min(mem_budget, total_budget)`, stop iterating.
6. No row is ever truncated mid-sentence — it is either fully injected or fully skipped.

### Adversarial Case

A single 5 000-token row (e.g. a 4 B worker dumping an entire file into `value`) exceeds the 1 200-token durable budget on its own. The algorithm **skips** it (the `total + t > budget` check fires before the row is appended). The result is an **empty** injection for that row — not a truncated fragment.

### Test Coverage

See `tests/test_token_budget.py` — verifies the 2 000-token cap under normal, adversarial, and boundary conditions.

---

## 11. Streaming Backpressure

### Zero-Application-Buffer Passthrough

`stream_to_vllm()` uses a raw async generator with **no application-level buffering**:

```python
async with client.stream("POST", VLLM_URL + "/chat/completions", json=vllm_body) as resp:
    async for line in resp.aiter_lines():
        if line.startswith("data: "):
            # ... optional usage-rewrite ...
            yield "data: " + data_str + "\n\n"
```

Each SSE line is `yield`ed immediately after it is read from the httpx response iterator. There is **no list, queue, or ring buffer** between vLLM and Goose.

### Backpressure Is TCP Flow Control

Because the proxy does not buffer, the only backpressure mechanism is the **OS TCP receive window**:

| Layer | Typical Buffer Size | Notes |
|-------|-------------------|-------|
| httpx (async) | ~64 KB | Per-connection read buffer; `aiter_lines()` drains it |
| Uvicorn (ASGI) | ~4 KB | Per-socket send buffer before `send()` blocks |
| OS TCP | 64–256 KB | Kernel receive/send windows (auto-tuned) |

When Goose reads slowly, the TCP window shrinks, Uvicorn's `send()` blocks, the async generator pauses, and httpx stops reading from vLLM. The entire chain stalls cooperatively — no data is dropped, no OOM risk.

### Failure Modes

| Scenario | What Happens |
|----------|-------------|
| **Goose disconnects mid-stream** | Uvicorn detects the broken pipe on next `send()`; the async generator raises; httpx closes the vLLM connection. The partial SSE stream is lost. |
| **vLLM 300 s timeout** | `httpx.TimeoutException` caught; proxy yields `data: [DONE]\n\n` and records the call as `timeout`. |
| **vLLM returns non-200** | Proxy reads the error body, yields it as a single SSE `error` event + `[DONE]`, records `vllm_{status}`. |
| **Goose is slow (backpressure)** | TCP window shrinks; the generator pauses; vLLM's TCP send buffer fills; vLLM slows its generation. No data loss, no timeout (as long as total time < 300 s). |

---

## 12. Tool-Call Validation Rules

### All-or-Nothing Per Message

`sanitize_tool_calls(message)` validates the **entire** `tool_calls` array. If **any** single item fails, the **entire** `tool_calls` key is stripped from the message. There is no per-item filtering.

### The 5 Rules

| # | Rule | Failure Example |
|---|------|----------------|
| 1 | Each item must be a `dict` | `tc = [42, {"id":"1"}]` → item 0 is `int` |
| 2 | Item must have `id`, `type`, `function` keys | `{"id":"1","function":{...}}` → missing `type` |
| 3 | `function` must have `name` and `arguments` | `{"id":"1","type":"function","function":{"name":"x"}}` → missing `arguments` |
| 4 | `arguments` must be `str` or `dict` | `"arguments": [1,2,3]` → is a list |
| 5 | If `arguments` is a `str`, it must parse via `json.loads` | `"arguments": "{bad json"` → `JSONDecodeError` |

### Concrete Malformed Examples

```json
// Rule 1: not a dict
"tool_calls": [42]

// Rule 2: missing "type"
"tool_calls": [{"id": "call_1", "function": {"name": "search", "arguments": "{}"}}]

// Rule 3: function missing "arguments"
"tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search"}}]

// Rule 4: arguments is a list
"tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": [1, 2]}}]

// Rule 5: unparseable JSON string
"tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{query: 'test'"}}]
```

### What Is NOT Checked

- The **value** of `function.name` (any string is accepted).
- The **literal value** of `type` (must be present but can be any string).
- **Argument size** — bounded only by the 20 MB HTTP body limit.
- **Schema validation** of arguments against the tool's JSON Schema.

### Effect on the Message

When validation fails:
1. The `tool_calls` key is **removed** from the message dict.
2. If `content` is missing or empty, it is set to `""` (vLLM requires at least one content field).
3. A `D9` warning is logged: `"D9: stripped malformed tool_calls from assistant message id=..."`.
4. The `toolcall_strips` metric is incremented.

---

## 13. Local Development Setup

### Docker Compose (PostgreSQL 16)

`docker-compose.yml` provisions a local PostgreSQL 16 instance with the project schema auto-applied on first start:

```yaml
services:
  postgres:
    image: postgres:16
    container_name: ctxproxy-pg
    ports: ["5432:5432"]
    environment:
      POSTGRES_USER: ctxproxy
      POSTGRES_PASSWORD: ctxproxy
      POSTGRES_DB: ctxproxy
    volumes:
      - ctxproxy-pgdata:/var/lib/postgresql/data
      - ./schema/001_init.sql:/docker-entrypoint-initdb.d/001_init.sql
      - ./schema/002_knowledge.sql:/docker-entrypoint-initdb.d/002_knowledge.sql
      - ./schema/003_memory_worker.sql:/docker-entrypoint-initdb.d/003_memory_worker.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ctxproxy"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  ctxproxy-pgdata:
```

The three schema files are mounted into `/docker-entrypoint-initdb.d/` so PostgreSQL applies them in order on the **first** container start (when the data volume is empty). Subsequent starts reuse the existing data.

### Makefile Targets

| Target | Command | Description |
|--------|---------|-------------|
| `make db` | `docker compose up -d postgres` | Start PostgreSQL |
| `make db-down` | `docker compose down` | Stop PostgreSQL |
| `make db-logs` | `docker compose logs -f postgres` | Tail PostgreSQL logs |
| `make test` | `pytest tests/ -v` | Run all tests |
| `make test-unit` | `pytest tests/ -v -k 'not e2e and not stress and not load'` | Fast unit tests only |
| `make lint` | `ruff check . && mypy proxy/ worker/` | Lint + type-check |
| `make run` | `python proxy/app.py` | Start the proxy |
| `make worker` | `python worker/worker.py` | Start the memory worker |
| `make clean` | `rm -rf __pycache__ .mypy_cache .pytest_cache *.pyc` | Remove build artifacts |

### Quick Dev Loop

```bash
# 1. Start the database (first time applies schema)
make db

# 2. Set environment (or use .env)
export CTXGATE_DB_DSN="postgresql://ctxproxy:ctxproxy@localhost:5432/ctxproxy"
export CTXGATE_VLLM_URL="http://127.0.0.1:29000/v1"
export CTXGATE_QWEN_TOKENIZER=" "   # space = skip tokenizer, use len//4

# 3. Run tests
make test-unit

# 4. Start the proxy
make run

# 5. In another terminal, start the worker
make worker

# 6. Point Goose at http://127.0.0.1:9200/v1
```

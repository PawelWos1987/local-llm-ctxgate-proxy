# local-llm-ctxgate-proxy Architecture

A lightweight local context gate proxy for long-running AI coding agents.

local-llm-ctxgate-proxy (Python/FastAPI, 3,046 LOC proxy + 708 LOC worker) sits between Goose (the AI agent) and vLLM (the LLM inference server). It enforces context-window boundaries, supplements context with conditional memory injection, and streams responses — all with < 5 ms added latency on the hot path.

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
| **Goose auto-compact** (safety gate) | Context reaches `0.90 x 84,000 = 75,600` | Last-resort summarization if the proxy's rolling window is insufficient |

Why the proxy is primary and Goose compact is the fallback:
- **The agent sees the full 84k window** and grows context freely — it does not self-limit.
- **The proxy trims what is forwarded to vLLM** on every request: 64k input + 18k output + 2k margin = 84k. Deterministic, lossy middle-message trimming at < 5 ms.
- **Goose compact at 0.90** is a coarser, lossy summarization that only runs if the proxy cannot keep the window in check — a backstop, not the steady-state path.
- **The 4B memory worker** supplies the durable facts that let the agent recover what the proxy trimmed, so the safety gate rarely needs to fire.

Goose compaction is the **index** — when it does run, it tells the agent which files and sessions to re-read. It keeps a single session oriented.

### 1.2 ctxgate-proxy (Context Boundary + Conditional Memory)

The proxy is a **deterministic gate** on every request:

1. **Token-aware assembly**: counts tokens using the Qwen tokenizer (not byte-estimates). Assembles the prompt from system + tools + messages, trimming oldest middle messages first when input exceeds MAX_INPUT (64,000 tokens).
2. **Conditional memory injection**: before sending to vLLM, checks if relevant memories exist in PostgreSQL that are NOT already in the current context. Uses a 60% overlap dedup gate — if the fact is already visible to the model, it is not re-injected.
3. **Prefix-cache safety**: memory is appended to the END of the system prompt (stable prefix, small suffix). The prefix fingerprint is computed from system + first user message; if it changes, a WARNING is logged and APC (Automatic Prefix Caching) is expected to miss.
4. **Transparent auto-continuation**: if vLLM hits `max_tokens` (finish_reason="length"), the proxy transparently continues generation up to 5 times (configurable via `CTXGATE_MAX_CONTINUATIONS`), so the agent never sees a broken truncation.
5. **Reasoning visibility**: `reasoning_content` is forwarded to the client in both stream and non-stream paths. The `--reasoning-parser qwen3` vLLM flag ensures the model's thinking is properly delimited.

### 1.3 4B Memory Worker (Async Durable Extraction)

A separate Python process (`worker/worker.py`, 708 LOC) that:
- Polls `proxy.memory_jobs` every 2 seconds (configurable)
- Calls the 4B model (qwen3-4b-instruct-2507 via LM Studio on port 1234) to extract structured facts
- Runs a **quality gate**: a second 4B call verifies no hallucination before storing
- Applies memories to `proxy.memories` with near-duplicate detection (token overlap >= 70%)
- Updates `proxy.working_memory` (current state + subtask)
- Recovers stuck `processing` jobs on startup (crash safety)
- Uses a **single-instance lock file** (`.worker.lock`) to prevent double-start
- Tracks outage state: if the 4B model is unreachable for > 30 min (`CTXGATE_WORKER_OUTAGE_TTL`), the worker backs off

The worker is **completely decoupled** from the request path. The proxy enqueues jobs via `asyncio.ensure_future` — zero added latency.

---

## 2. Request Flow (Hot Path)

```
Goose Desktop
    |
    |  POST /v1/chat/completions
    v
+----------------------------------------------------------------+
| ctxgate-proxy (port 9200)                                      |
|                                                                |
|  1. Auth check (optional API key)                               |
|  2. Body size guard (MAX_BODY_BYTES)                            |
|  3. Session key derivation (X-Session-ID + content fingerprint) |
|  4. Memory job enqueue (fire-and-forget, non-blocking)          |
|  5. Task resolution (proxy.tasks <-> Goose sessions.db)        |
|  6. build_context():                                           |
|     a. sanitize_for_vllm (strip reasoning, fix tool_calls)     |
|     b. count_messages_tokens (Qwen tokenizer)                  |
|     c. trim_context if > MAX_INPUT (FIFO, protect system+last) |
|     d. Fire-and-forget: summarize trimmed messages via 4B      |
|  7. Knowledge + memory injection (parallel, asyncio.gather):   |
|     a. fetch_relevant_knowledge (global, cross-session)        |
|     b. fetch_task_memory (per-task: WM + summary + memories)  |
|  8. Prefix fingerprint check (APC invalidation detection)      |
|  9. max_tokens calculation (proxy owns this, post-trim)        |
| 10. Forward to vLLM (stream or non-stream)                     |
| 11. Response: usage includes prompt_tokens_details.cached_tokens|
+----------------------------------------------------------------+
    |
    |  SSE stream / JSON response
    v
Goose Desktop (sees cached_tokens in message summary)
```

**Total added latency: < 5 ms** (token counting + trim + injection). The dominant cost is vLLM inference itself.

---

## 3. vLLM Configuration

The proxy expects vLLM to be running with these critical flags:

| Flag | Purpose |
|---|---|
| `--enable-prefix-caching` | APC — reuses KV cache for identical prefixes (system prompt + tools) |
| `--enable-prompt-tokens-details` | Returns `prompt_tokens_details.cached_tokens` in usage |
| `--enable-chunked-prefill` | Splits long prompts into chunks for faster TTFT |
| `--speculative-config` | DFlash2 speculative decoding (3 tokens) — ~30% speedup |
| `--reasoning-parser qwen3` | Properly delimits `reasoning_content` from `content` |
| `--enable-auto-tool-choice` | Auto-detects tool calls without explicit `tool_choice` |
| `--tool-call-parser qwen3_coder` | Qwen3-specific tool call format |
| `--kv-cache-dtype fp8` | Halves KV cache memory, enables longer contexts |
| `--max-model-len 84000` | Matches proxy's MAX_CONTEXT |
| `--max-num-seqs 1` | Single-stream (Goose is single-agent) |
| `--performance-mode interactivity` | Optimizes for low latency over throughput |

The proxy pings `/v1/models` every 60s to track vLLM availability. If vLLM is down, requests are rejected early with a 503 (no timeout waste).

---

## 4. Token Budget

| Parameter | Default | Env Var |
|---|---|---|
| MAX_CONTEXT | 84,000 | `CTXGATE_MAX_CONTEXT` |
| MAX_INPUT | 64,000 | `CTXGATE_MAX_INPUT` |
| MAX_OUTPUT | 18,000 | `CTXGATE_MAX_OUTPUT` |
| SAFETY_MARGIN | 2,000 | `CTXGATE_SAFETY_MARGIN` |
| WALL_CLOCK_MAX | 1,800s | `CTXGATE_WALL_CLOCK_MAX` |
| MAX_CONTINUATIONS | 5 | `CTXGATE_MAX_CONTINUATIONS` |

The proxy **owns** the `max_tokens` calculation: `min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)`. Goose's `max_tokens` is ignored (it's based on pre-trim input).

---

## 5. Session & Task Model

### 5.1 Session Key Derivation

```
session_key = "{x_session_id}:{content_fingerprint}"
```

- `x_session_id`: from `X-Session-ID` header (static per Goose provider)
- `content_fingerprint`: SHA-256 of (system prompt + first user message), truncated to 8 hex chars

This means: same provider + same conversation topic = same key. Same provider + different topic = different key.

### 5.2 Task Resolution

`proxy.tasks` maps 1:1 to Goose sessions:
- `session_id` = Goose session ID (from `X-Session-ID` or SQLite fallback)
- Enriched with: `name`, `session_type`, `working_dir`, `provider_name` (lazy from Goose SQLite DB)
- `ON CONFLICT (session_id) DO UPDATE` for idempotent creation

### 5.3 Prefix Fingerprint

Computed from system + first user message. If it changes between requests for the same session_key, a `PREFIX INVALIDATED` warning is logged. This indicates APC will miss on the next request.

---

## 6. Memory Architecture (PostgreSQL)

### 6.1 Schema (5 migration files)

| File | Tables/Changes |
|---|---|
| `001_init.sql` | `proxy.tasks`, `proxy.events`, `proxy.memories`, `proxy.working_memory`, `proxy.memory_jobs` |
| `002_knowledge.sql` | `proxy.knowledge` (global, cross-session) |
| `003_memory_worker.sql` | Adds `source_event_id`, `status`, `attempts`, `model_name` columns |
| `004_memory_ttl.sql` | Adds `last_accessed_at`, `expires_at` to memories |
| `005_seed_knowledge.sql` | Seed data for knowledge table |

### 6.2 Table Summary

| Table | Scope | Purpose |
|---|---|---|
| `proxy.tasks` | Per Goose session | Identity + metadata (name, working_dir, provider) |
| `proxy.events` | Per task | Ordered conversation events (for memory worker context) |
| `proxy.memories` | Per task | Durable facts (key, value, importance, TTL, status) |
| `proxy.working_memory` | Per task | Current state + subtask (single row) |
| `proxy.memory_jobs` | Per task | Queue for 4B worker (pending -> processing -> done/failed) |
| `proxy.knowledge` | **Global** | Cross-session knowledge (domain, key, value, importance) |

### 6.3 Memory Injection Priority

On each request, `fetch_task_memory` assembles (in priority order):
1. **Working memory** (current state) — 800 token budget
2. **Session summary** (from trimmed-message summarization) — 600 token budget
3. **Critical memories** (importance=10) — 5 most recent
4. **Relevant memories** (term-matched) — 8 most relevant

Total budget: <= 2,000 tokens. Items already visible in the current context are **not** re-injected (60% token overlap gate).

### 6.4 Knowledge (Cross-Session)

`proxy.knowledge` is **global** (no task FK). Extracted by the 4B model in a background fire-and-forget pipeline:
1. **Generate**: 4B extracts 0-3 items from last 6 messages
2. **Verify**: 4B judges quality (hallucination check)
3. **Retry**: bad items regenerated with explicit quality instructions
4. **Re-verify**: second quality check
5. **Delete**: items failing twice are discarded

On injection, `fetch_relevant_knowledge` does term-based matching (ILIKE) against the global table, limited to 5 items / 400 tokens.

---

## 7. Auto-Continuation

When vLLM returns `finish_reason="length"` (hit max_tokens):

**Stream path:**
- Proxy appends the partial assistant response + a "Continue" user message
- Re-trims if context grew over MAX_INPUT
- Re-submits to vLLM (up to `MAX_CONTINUATIONS` times)
- Wall clock cap: `WALL_CLOCK_MAX` (default 1800s)

**Non-stream path:**
- Same logic, but also handles **reasoning overflow** detection:
  - If `reasoning_content` >= 12,000 tokens and `content` < 50 chars → the model spent all budget thinking
  - Fix: re-submit with `enable_thinking=false` to get actual content
- Tool call truncation detection: invalid JSON in `arguments` → classified as `tool_call_truncation`

---

## 8. Response Usage (APC Visibility)

The proxy ensures `prompt_tokens_details.cached_tokens` is **always present** in the response usage:

**Stream:** The final chunk always includes:
```json
{"usage": {"prompt_tokens": N, "completion_tokens": M, "total_tokens": N+M, "prompt_tokens_details": {"cached_tokens": K}}}
```

**Non-stream:** The response `data["usage"]` always has `prompt_tokens` explicitly set (survives continuation mutations).

This is what Goose Desktop reads to display the cache hit indicator in the message summary.

---

## 9. Process Management

### 9.1 Proxy (supervisor.sh)

- **Lock file** (`supervisor.lock`): prevents double-start
- **Stale port kill**: at startup, kills any process on port 9200
- **Backoff**: clean exit → 1s; crash > 5s → 2s; 3+ consecutive crashes → 10s
- **PID file** (`proxy.pid`): for external monitoring

### 9.2 Worker (standalone)

- **Single-instance lock** (`worker/.worker.lock`): PID + timestamp, stale lock detection
- **sd_notify**: sends `READY=1` for systemd integration
- **Stuck job recovery**: on startup, resets all `processing` → `pending`
- **Graceful shutdown**: SIGTERM/SIGINT → close pool, release lock

### 9.3 Startup Order

```
1. PostgreSQL (systemd / docker)
2. vLLM (vllm-serve.fish)
3. LM Studio (4B model, port 1234)
4. ctxgate-proxy (supervisor.sh)
5. worker (worker/worker.py)
6. Goose (connects to proxy:9200)
```

The proxy retries DB connection for up to 120s (60 x 2s). The worker does the same.

---

## 10. API Routes (22 total)

### Core (1)
| Route | Method | Purpose |
|---|---|---|
| `/v1/chat/completions` | POST | Main proxy endpoint (stream + non-stream) |

### Health & Metrics (4)
| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness probe |
| `/ready` | GET | Readiness (DB + vLLM) |
| `/metrics` | GET | JSON metrics |
| `/metrics/prometheus` | GET | Prometheus format |

### API (7)
| Route | Method | Purpose |
|---|---|---|
| `/api/metrics` | GET | Detailed metrics |
| `/api/sessions` | GET | Active sessions with token counts |
| `/api/recent-calls` | GET | Ring buffer of recent calls |
| `/api/errors` | GET | Recent errors |
| `/api/memory-summary` | GET | Memory stats per task |
| `/api/memory` | GET | Memory entries (filter by session) |
| `/api/memory-analytics` | GET | Memory analytics (injection rates) |

### Knowledge (3)
| Route | Method | Purpose |
|---|---|---|
| `/knowledge` | POST | Create knowledge item |
| `/knowledge/search` | GET | Search knowledge |
| `/knowledge/stats` | GET | Knowledge stats |

### Memory (2)
| Route | Method | Purpose |
|---|---|---|
| `/memory/inject` | POST | Manual memory injection |
| `/memory/{task_ref}` | GET | Query memories for a task |

### System (2)
| Route | Method | Purpose |
|---|---|---|
| `/api/lmstudio` | GET | LM Studio status |
| `/api/gpu` | GET | GPU stats |

### Dashboard (1)
| Route | Method | Purpose |
|---|---|---|
| `/dashboard` | GET | HTML dashboard |

### Test (2)
| Route | Method | Purpose |
|---|---|---|
| `/_test/reset_prefix` | POST | Reset prefix fingerprints |
| `/_test/reset_sessions` | POST | Reset session state |

---

## 11. Environment Variables

### Proxy
| Var | Default | Purpose |
|---|---|---|
| `CTXGATE_DB_DSN` | — | PostgreSQL connection string |
| `CTXGATE_VLLM_URL` | `http://127.0.0.1:29000/v1` | vLLM base URL |
| `CTXGATE_VLLM_MODEL` | `Qwen3.8-27B` | Model name for vLLM requests |
| `CTXGATE_LM_URL` | `http://127.0.0.1:1234/v1` | 4B model URL (LM Studio) |
| `CTXGATE_LM_MODEL` | `qwen3-4b-instruct-2507` | 4B model name |
| `CTXGATE_LM_TIMEOUT` | 120 | 4B call timeout (seconds) |
| `CTXGATE_QWEN_TOKENIZER` | — | Path to tokenizer.json |
| `CTXGATE_MAX_CONTEXT` | 84000 | Total context window |
| `CTXGATE_MAX_INPUT` | 64000 | Max input tokens (trim threshold) |
| `CTXGATE_MAX_OUTPUT` | 18000 | Max output tokens |
| `CTXGATE_SAFETY_MARGIN` | 2000 | Reserved tokens |
| `CTXGATE_WALL_CLOCK_MAX` | 1800 | Max wall-clock per request (seconds) |
| `CTXGATE_MAX_CONTINUATIONS` | 5 | Max auto-continuation retries |
| `CTXGATE_PROXY_PORT` | 9200 | Listen port |
| `CTXGATE_API_KEY` | — | Optional Bearer auth |
| `CTXGATE_MAX_BODY_BYTES` | 50MB | Request body size limit |
| `CTXGATE_MEMORY_WORKER` | 1 | Enable/disable memory job enqueue |
| `GOOSE_SESSIONS_DB` | `~/.local/share/goose/sessions/sessions.db` | Goose SQLite path |

### Worker
| Var | Default | Purpose |
|---|---|---|
| `CTXGATE_DB_DSN` | — | PostgreSQL connection string |
| `CTXGATE_LM_URL` | — | 4B model URL |
| `CTXGATE_LM_MODEL` | — | 4B model name |
| `CTXGATE_LM_BASE` | — | 4B base URL (alternative) |
| `CTXGATE_WORKER_POLL` | 2.0 | Poll interval (seconds) |
| `CTXGATE_WORKER_MAX_ATTEMPTS` | 3 | Max retry attempts per job |
| `CTXGATE_WORKER_OUTAGE_TTL` | 1800 | Outage backoff (seconds) |
| `CTXGATE_WORKER_CONCURRENCY` | 1 | Concurrent job processing |
| `CTXGATE_WORKER_MAX_TOKENS` | 2048 | Max tokens per 4B call |
| `CTXGATE_MEMORY_TTL_DAYS` | 90 | Memory TTL (days) |
| `CTXGATE_WORKER_LOCK` | — | Lock file path |
| `CTXGATE_WORKER_LOCK_TTL` | — | Stale lock threshold |

---

## 12. File Structure

```
ctxproxy/
+-- proxy/
|   +-- app.py              # 3,046 LOC - main proxy (FastAPI)
|   +-- patch_fixes.py      # One-off migration patches
|   +-- requirements.txt
+-- worker/
|   +-- worker.py           # 708 LOC - 4B memory worker
|   +-- .worker.lock        # Single-instance lock (runtime)
+-- schema/
|   +-- 001_init.sql        # Core tables (tasks, events, memories, WM, jobs)
|   +-- 002_knowledge.sql   # Global knowledge table
|   +-- 003_memory_worker.sql  # Worker columns (source_event_id, status, etc.)
|   +-- 004_memory_ttl.sql  # TTL columns (last_accessed_at, expires_at)
|   +-- 005_seed_knowledge.sql  # Seed data
+-- dashboard/
|   +-- dashboard.py        # HTML dashboard generator
+-- tests/                  # 28 test files
+-- .env                    # Runtime configuration
+-- supervisor.sh           # Proxy process manager (lock + backoff)
+-- start_proxy.sh          # Manual start script
+-- run_proxy.sh            # Alternative start script
+-- cutover.sh              # Production cutover script
+-- ARCHITECTURE.md         # This file
+-- README.md
+-- NOTES.md                # Session state
+-- Makefile
```

---

## 13. Design Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Proxy owns max_tokens (post-trim) | Goose's max_tokens is based on pre-trim input — always too small |
| D2 | FIFO trim (oldest first, protect system + last user) | Simple, deterministic, preserves most recent context |
| D3 | Memory at END of system prompt | Stable prefix for APC; small suffix doesn't invalidate cache |
| D4 | 60% overlap dedup gate | Prevents re-injecting facts already visible to the model |
| D5 | Separate worker process | Zero coupling to request path; crash isolation |
| D6 | Single-instance lock (both proxy + worker) | Prevents port conflicts and double-processing |
| D7 | Fire-and-forget memory enqueue | `asyncio.ensure_future` — adds 0ms to request path |
| D8 | Qwen tokenizer (not tiktoken) | Exact token counts matching vLLM's internal counting |
| D9 | Reasoning content forwarded | User wants to see thinking in Goose Desktop |
| D10 | Auto-continuation (up to 5x) | Agent never sees broken truncation |
| D11 | Per-session key (provider + content fp) | Same provider + same topic = same session; different topic = new session |
| D12 | prompt_tokens_details always present | Goose Desktop needs it for cache display; was conditionally omitted |
| D13 | Wall clock cap (1800s) | Prevents infinite generation loops on stuck models |
| D14 | 4B quality gate (2-pass) | Prevents hallucinated memories from being stored |
| D15 | Knowledge is global (no task FK) | Cross-session sharing is the point |

---

## 14. Performance Characteristics

| Metric | Target | Measured |
|---|---|---|
| Proxy added latency (hot path) | < 5 ms | 2-4 ms (token count + trim + injection) |
| DB query latency (injection) | < 10 ms | 3-8 ms (local PostgreSQL) |
| Token counting (64k tokens) | < 50 ms | 15-30 ms (Qwen tokenizer, Rust backend) |
| Memory job enqueue | < 5 ms | 1-3 ms (single INSERT) |
| 4B extraction (per job) | < 60 s | 20-40 s (2 LLM calls) |
| vLLM TTFT (with APC hit) | < 2 s | 1-3 s (chunked prefill) |
| vLLM TTFT (APC miss) | < 5 s | 3-8 s (full prefill) |

The proxy is **not** the bottleneck. vLLM inference (27B model, 2xTP) dominates. The proxy's job is to ensure the right tokens reach vLLM with minimal waste.

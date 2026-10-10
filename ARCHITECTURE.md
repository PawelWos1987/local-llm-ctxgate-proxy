# ctxgate-proxy Architecture

Technical source of truth for the public implementation. All claims below are derived directly from the code in `proxy/app.py` (4,114 LOC), `worker/worker.py`, `dashboard/dashboard.py`, and the schema files.

---

## 1. Components

| Component | File | Process | Port | Role |
|---|---|---|---|---|
| **Proxy** | `proxy/app.py` | Main | 9201 | Request interception, token counting, trimming, memory injection, streaming |
| **Worker** | `worker/worker.py` | Separate | - | Async memory extraction from PostgreSQL job queue |
| **Dashboard** | `dashboard/dashboard.py` | Separate | 9202 | Health monitoring, metrics display, service control |
| **vLLM** | External | External | 29000 | Main LLM inference (Qwen3.8-27B) |
| **Mistral API** | External (cloud) | - | - | Helper LM for summaries, memory extraction, knowledge |
| **PostgreSQL** | External | External | 5432 | Persistent storage (memories, events, jobs, knowledge, sessions) |

---

## 2. Request Lifecycle (Hot Path)

```
Agent POST /v1/chat/completions
    |
    v
[1] Parse request (OpenAI-compatible JSON)
    |
    v
[2] Token count all messages (Qwen tokenizer)
    |
    v
[3] Session key derivation (X-Session-Id header + prefix fingerprint)
    |
    v
[4] Frozen prefix check (first 3 messages immutable per session)
    |   ^ If prefix changed -> new session key, KV-cache invalidated
    v
[5] Trim to MAX_INPUT tokens
    |   - Drop oldest MIDDLE messages first
    |   - System prompt: NEVER touched
    |   - Newest user message: NEVER touched
    |   - Tool call messages: stripped of tool_results
    v
[6] Memory injection (if available)
    |   - Working memory (STATE/SUBTASK line)
    |   - Task-scoped durable memories
    |   - Knowledge (shared facts)
    |   - Appended to LAST USER MESSAGE only
    |   - 60% overlap dedup gate
    v
[7] Forward to vLLM (streaming passthrough)
    |
    v
[8] Post-request: event capture to proxy.events
    |
    v
[9] Async: enqueue trim summary + knowledge extraction
```

**Added latency on hot path: < 5 ms** (token counting + trim logic only; no external calls).

---

## 3. Context Management

### 3.1 Frozen Prefix (KV-Cache Stability)

The first 3 messages of a session are **immutable**:

| Position | Role | Content |
|---|---|---|
| 0 | system | Agent system prompt |
| 1 | user | Seed user message |
| 2 | assistant | Seed assistant response |

These form the **session prefix fingerprint** (SHA-256 of concatenated raw text). If any of the 3 messages changes between requests, the session key is invalidated and vLLM's prefix cache is reset. This guarantees that the KV-cache for the prefix is never invalidated mid-session.

**Injections are appended to the last user message only.** The system prompt is never mutated.

### 3.2 Rolling-Window Trim

When total input tokens exceed `MAX_INPUT` (default 64,000):

1. Calculate overflow = total_tokens - MAX_INPUT
2. Walk messages from index 3 onward (after frozen prefix)
3. Drop oldest messages first (FIFO within the mutable region)
4. **Never drop**: message[0] (system), message[1] (seed user), message[2] (seed assistant), last user message
5. Tool call messages have their `tool_results` arrays stripped before counting

The trim is **deterministic and lossy** - dropped messages are gone from the vLLM request. Their content is preserved in `proxy.events` (PostgreSQL) and summarized by the async pipeline.

### 3.3 Token Budget

```
MAX_CONTEXT (84,000) = MAX_INPUT (64,000) + SAFETY_MARGIN (2,000) + MAX_OUTPUT (18,000)
```

Validated at startup by `validate_config()`. If the constraint is violated, the process exits immediately.

### 3.4 Hierarchical Summarization

When messages are trimmed, the proxy enqueues a **two-phase summarization**:

- **Phase 1**: Generate a concise summary of the trimmed messages (per phase, stored in `proxy.phase_summaries`)
- **Phase 2**: Distill all phase summaries into a single root summary (stored in `proxy.session_summaries`, capped at 6,000 chars)

These summaries are injected into the next request's context, giving the model a compressed view of what was trimmed.

---

## 4. Session Model

- **Session key**: Derived from `X-Session-Id` header (if present) + prefix fingerprint hash
- **In-memory state**: `session_seeds`, `session_compactions`, `session_tokens` dicts (lost on restart)
- **Persistent state**: PostgreSQL `proxy.tasks`, `proxy.events`, `proxy.memories`, `proxy.phase_summaries`, `proxy.session_summaries`
- **Isolation**: Each session has its own memory scope. Memories are task-scoped and only injected for matching sessions.
- **Eviction**: In-memory session state is evicted when memory pressure is high (LRU). Persistent state in PostgreSQL is unaffected.

---

## 5. Memory System

### 5.1 Architecture

```
proxy/app.py (hot path)
    |
    | enqueue job (async, non-blocking)
    v
proxy.memory_jobs (PostgreSQL)
    |
    | FOR UPDATE SKIP LOCKED (poll every 2s)
    v
worker/worker.py (separate process)
    |
    | Mistral API call (strict JSON schema)
    v
proxy.memories (PostgreSQL)
    |
    | injected into last user message on next request
    v
proxy/app.py (hot path)
```

### 5.2 Worker Design

- **Single-instance guard**: `fcntl.flock` on a lock file + heartbeat file (30s TTL). If the holder is frozen (alive but not progressing), a replacement takes over.
- **Concurrency**: 1 worker process, 1 job at a time (parallelism=1). Uses `FOR UPDATE SKIP LOCKED` for safe access.
- **Outage handling**: If Mistral API is unreachable, jobs stay pending with exponential backoff for up to `CTXGATE_WORKER_OUTAGE_TTL` (default 1800s = 30 min). After that, jobs are marked failed.
- **Quality check**: After extraction, a second Mistral call (temp=0.1, 256 tokens) validates the output for hallucination. If it fails, 1 retry; if it fails again, the job is discarded.
- **No loops**: The worker's output NEVER enqueues a new memory job. Only original events (from the proxy) create jobs.

### 5.3 Memory Schema

| Field | Values |
|---|---|
| `action` | NEW, UPDATE, SUPERSEDE, DUPLICATE, NO_CHANGE |
| `type` | DECISION, FINDING, FAILURE, TODO, CONSTRAINT, FILE, STATE, FACT |
| `importance` | CRITICAL, HIGH, NORMAL, LOW |
| `status` | active, superseded, pruned |
| `key` | Normalized title (used for dedup) |
| `value` | The fact/content |
| `task_id` | Session/task scope |
| `source_event_id` | Traceability to original event |

### 5.4 TTL Pruning

Non-critical (importance < HIGH), never-reused memories are pruned after `CTXGATE_MEMORY_TTL_DAYS` (default 90 days). Pruning sets `status='pruned'` (soft delete, not row removal).

### 5.5 Injection Logic

On each request:
1. Fetch active memories for the current task_id
2. Compute overlap with current context (token-level, 60% threshold)
3. If overlap < 60%, inject the memory into the last user message
4. Working memory (STATE/SUBTASK) is always injected first
5. Knowledge (shared facts) is injected after task memories

---

## 6. Knowledge System

- **Storage**: `proxy.knowledge` table (shared across all tasks)
- **Extraction**: Enqueued as P2 priority (can wait up to 1 hour)
- **Aging**: P2 tasks waiting > 120s are bumped to P1 priority
- **Injection**: Shared facts injected into the last user message
- **Use case**: Project-level facts that apply across all sessions (e.g., "the project uses Python 3.11", "the API is at /v2")

---

## 7. Helper LM (Mistral API)

### 7.1 Role

The helper model handles **all non-chat-completion LLM tasks**:
- Trim/phase summarization
- Memory extraction (worker)
- Knowledge extraction
- Quality checks

It is **never in the hot path**. All helper calls are async and non-blocking.

### 7.2 Priority Queue

```
+---------------------------+
|  asyncio.PriorityQueue    |
|  (shared, 2 consumers)    |
+---------------------------+
         |           |
    Consumer 1   Consumer 2
         |           |
         v           v
    Mistral API (max 2 concurrent requests)
```

| Priority | Task Type | Aging |
|---|---|---|
| P0 (0) | Trim summaries (feeds working memory) | None |
| P1 (1) | Aged knowledge tasks (> 120s wait) | Auto-bumped from P2 |
| P2 (2) | Knowledge extraction | Bumps to P1 after 120s |

### 7.3 Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CTXGATE_LM_URL` | `https://api.mistral.ai/v1` | Base URL |
| `CTXGATE_LM_MODEL` | `mistral-small-latest` | Model name |
| `CTXGATE_LM_API_KEY` | - | API key |
| `CTXGATE_LM_TIMEOUT` | 120 | Request timeout (seconds) |

---

## 8. Background Loops

| Loop | Location | Interval | Purpose |
|---|---|---|---|
| Watchdog | `proxy/app.py` | 10s | Ping systemd NOTIFY_SOCKET (deadlock detection) |
| Memory job recovery | `proxy/app.py` | 5s | Reset stuck jobs (processing > 120s) |
| Worker poll | `worker/worker.py` | 2s | Pick up pending memory jobs |
| Worker heartbeat | `worker/worker.py` | 5s | Write status file (lag, heartbeat) |
| TTL prune | `worker/worker.py` | 1h | Prune expired memories |
| Dashboard poll | `dashboard/dashboard.py` | 3s | Refresh health/metrics display |

---

## 9. Resilience & Failure Modes

### 9.1 Circuit Breakers

| Breaker | Threshold | Cooldown | Behavior when open |
|---|---|---|---|
| vLLM | 5 consecutive failures | 30s | Immediate 503 (fail-fast) |
| LM (Mistral) | 5 consecutive failures | 30s | Async jobs return empty; hot path unaffected |

Half-open state: after cooldown, one probe request is allowed. Success closes the breaker; failure re-opens it.

### 9.2 Fail-Fast Validation

`validate_config()` runs at startup and **exits the process** if:
- `CTXGATE_DB_DSN` is unparseable or has no hostname
- `CTXGATE_VLLM_URL` is not a valid http(s) URL
- `MAX_INPUT + SAFETY_MARGIN > MAX_CONTEXT`
- `MAX_OUTPUT < 1`

### 9.3 Systemd Integration

- `NOTIFY_SOCKET` support (sd_notify READY=1, WATCHDOG=1)
- SIGTERM handler logs the sender PID before terminating
- Watchdog: 10s ping, 60s kill threshold (detects event-loop deadlocks)

### 9.4 Worker Single-Instance

- `fcntl.flock` on `worker/.worker.lock` (kernel releases on process death)
- Heartbeat file (5s interval, 30s staleness threshold)
- Stale takeover: if heartbeat is > 30s old, a new process can acquire the lock

---

## 10. Startup & Shutdown

### Startup Sequence

```
1. Load .env (project root, then cwd; real env vars take precedence)
2. Install SIGTERM handler (log sender, chain to default)
3. validate_config() -> exit(1) on failure
4. Connect to PostgreSQL (asyncpg pool)
5. Start watchdog loop (asyncio task)
6. Start LM priority queue consumers (2 tasks)
7. Start memory job recovery loop
8. Start uvicorn (FastAPI app on :9201)
9. sd_notify READY=1
```

### Shutdown

- SIGTERM -> log sender -> uvicorn graceful shutdown -> pool close -> exit
- Worker: SIGTERM -> finish current job -> release flock -> exit

---

## 11. Configuration

All configuration is via **environment variables** (no YAML at runtime; `config.example.yaml` is for reference/documentation only).

| Category | Variables |
|---|---|
| Database | `CTXGATE_DB_DSN` |
| vLLM | `CTXGATE_VLLM_URL`, `CTXGATE_VLLM_MODEL` |
| Helper LM | `CTXGATE_LM_URL`, `CTXGATE_LM_MODEL`, `CTXGATE_LM_API_KEY`, `CTXGATE_LM_TIMEOUT` |
| Tokenizer | `CTXGATE_QWEN_TOKENIZER` |
| Context budget | `CTXGATE_MAX_CONTEXT`, `CTXGATE_MAX_INPUT`, `CTXGATE_SAFETY_MARGIN`, `CTXGATE_MAX_OUTPUT` |
| Worker | `CTXGATE_WORKER_POLL`, `CTXGATE_WORKER_MAX_ATTEMPTS`, `CTXGATE_WORKER_OUTAGE_TTL`, `CTXGATE_WORKER_MAX_TOKENS` |
| Memory | `CTXGATE_MEMORY_TTL_DAYS` |
| Sessions | `GOOSE_SESSIONS_DB` (path to Goose sessions SQLite) |
| Worker lock | `CTXGATE_WORKER_LOCK`, `CTXGATE_WORKER_LOCK_TTL` |
| Worker status | `CTXGATE_WORKER_STATUS` |

---

## 12. What Is Implemented vs. Not

### Implemented (in current code)

- [x] Token-aware rolling-window trim (Qwen tokenizer)
- [x] Frozen 3-message prefix (KV-cache stability)
- [x] Memory injection into last user message (60% dedup gate)
- [x] Async memory extraction (separate worker process)
- [x] Hierarchical phase summarization (2-phase)
- [x] Knowledge sharing (cross-task facts)
- [x] Working memory (STATE/SUBTASK line)
- [x] Priority queue for helper LM (2 consumers, aging)
- [x] Circuit breakers (vLLM + LM)
- [x] Fail-fast config validation
- [x] Systemd watchdog + sd_notify
- [x] Worker single-instance (flock + heartbeat)
- [x] TTL pruning (90 days)
- [x] Event capture (all requests to PostgreSQL)
- [x] Dashboard (health, metrics, service control)
- [x] Streaming passthrough
- [x] Session isolation (task-scoped memories)
- [x] Docker compose deployment
- [x] CI (pytest + CodeQL)

### Configuration-Dependent

- Token counting accuracy (requires Qwen tokenizer.json)
- Helper LM availability (Mistral API key required)
- Dashboard availability (separate process, optional)
- Systemd integration (requires NOTIFY_SOCKET)

### Intentionally NOT Implemented

- **Authentication/authorization**: No auth layer. Use a reverse proxy.
- **Multi-node/distributed state**: Single-host design. In-memory session state is not shared.
- **In-context summarization**: The proxy does NOT summarize in the hot path. Summaries are async and injected on the next request.
- **Model routing**: All chat completions go to the single configured vLLM endpoint. No model selection/routing.
- **Prompt engineering**: The proxy does not modify the system prompt or add instructions. It only appends memory to the last user message.
- **Rate limiting**: No client-side rate limiting. Circuit breakers protect the backend, not the clients.

---

## 13. Design Decisions

| Decision | Rationale |
|---|---|
| Frozen prefix (3 messages) | KV-cache stability is the #1 latency factor. Mutating the system prompt invalidates the entire prefix cache. |
| Memory in last user message, not system | Same reason. The system prompt must be byte-identical across all requests in a session. |
| Separate worker process | Memory extraction is slow (2-10s per job). It must never block the hot path. A separate process with its own event loop is the cleanest isolation. |
| FOR UPDATE SKIP LOCKED | Safe concurrent access without row-level locking contention. If a job is being processed, the next poll skips it. |
| Mistral API (not local) for helper | The helper model only needs to be good at structured extraction. A small cloud model is cheaper and more reliable than running a second local model. |
| Priority queue with aging | Trim summaries are time-sensitive (feed the next request). Knowledge extraction can wait. Aging prevents starvation. |
| No auth in proxy | The proxy is designed for localhost. Adding auth complicates the OpenAI-compatible interface. Use a reverse proxy for network exposure. |
| Soft delete (status column) | Preserves audit trail. Pruned memories can be restored. Row deletion loses the supersession chain. |
| flock + heartbeat (not DB lock) | Kernel-level lock is released on process death (no stale locks). Heartbeat handles the "frozen but alive" case. |

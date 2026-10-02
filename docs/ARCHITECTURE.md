# Architecture

local-llm-ctxgate-proxy is a context gateway that sits between LLM clients
(Goose, custom apps) and a local vLLM server, optimizing KV-cache hit rates
through session-aware context management.

## System Overview

```
                    +------------------+
                    |   LLM Client     |
                    |  (Goose, app)   |
                    +--------+--------+
                             |
                             v
                    +------------------+     +------------------+
                    |  ctxgate-proxy   |<-->|     vLLM        |
                    |  (FastAPI:9201)  |     |  (:29000)       |
                    +--------+--------+     +------------------+
                             |
              +--------------+--------------+
              |              |              |
              v              v              v
     +--------------+  +-----------+  +------------------+
     | PostgreSQL    |  | LM Studio |  |  4B Memory       |
     | (:5432)       |  | (:1234)   |  |  Worker          |
     | memories,     |  | 4B model  |  |  (worker.py)     |
     | knowledge,    |  |           |  |                  |
     | tasks, events |  |           |  |                  |
     +--------------+  +-----------+  +------------------+
              |
              v
     +------------------+
     |  Dashboard       |
     |  (FastAPI:9202)  |
     |  Control Room    |
     +------------------+
```

## Core Components

### 1. Proxy (proxy/app.py, ~3400 lines)

The heart of the system. A FastAPI application that:

- **Receives** OpenAI-compatible chat completion requests
- **Manages sessions**: Each unique (system+first-user) hash gets a session
  key. Messages are accumulated per session.
- **Optimizes KV-cache**: By maintaining a frozen prefix of messages
  (the "session seed"), vLLM can reuse its KV-cache across requests in
  the same session. The prefix is immutable except for logged re-freeze
  events.
- **Compacts context**: When the context exceeds max_context_tokens,
  older messages are summarized (via the 4B worker) and replaced with
  a compact summary, preserving the most recent messages.
- **Normalizes messages**: Merges leading consecutive system messages
  into one (vLLM requires system at position 0).
- **Tracks metrics**: Token-weighted cache hit rate, request counts,
  latency, session stats. Exposed via /api/metrics and /metrics (Prometheus).

Key data structures:
- `session_seeds[key]`: Frozen prefix (immutable)
- `session_compactions[key]`: List of compaction summaries
- `SESSION_LAST_ACTIVE[key]`: Timestamp for TTL eviction
- `recent_calls`: Deque ring buffer (last N requests)

### 2. 4B Memory Worker (worker/worker.py, ~750 lines)

A background process that:

- **Polls** PostgreSQL for pending memory jobs (created by the proxy)
- **Calls** a 4B model (LM Studio) to extract memories from conversation events
- **Applies** memory actions (NEW, UPDATE, SUPERSEDE, NO_CHANGE) to the
  memories table using deterministic dedup via key_norm
- **Updates** working memory (current state + subtask) per task
- **Tracks** health via atomic status file (lag, heartbeat, jobs done)
- **Self-heals**: Resets model_loaded after 3 consecutive LM failures,
  recovers stuck 'processing' jobs on restart

Single-instance enforcement via fcntl lock file.

### 3. Dashboard (dashboard/dashboard.py, ~990 lines)

A real-time control room:

- **Health monitoring**: Polls PostgreSQL, vLLM, LM Studio, proxy, worker
- **Visualization**: Animated SVG fiber-optic topology with status LEDs
- **Control**: Start/stop/restart services (requires DASH_TOKEN auth)
- **Configuration**: Edit config.yaml from the GUI (atomic writes, rate-limited)
- **Hot-reload**: Watches config.yaml mtime, rebuilds service map on change
- **systemd integration**: sd_notify for watchdog (WatchdogSec=30)

### 4. PostgreSQL Schema (schema/001-008)

| Migration | Purpose |
|-----------|---------|
| 001 | Core tables: tasks, events, memories, memory_jobs |
| 002 | Knowledge base (shared, cross-session) |
| 003 | Memory worker columns: source_event_id, status |
| 004 | Memory TTL: last_accessed_at, expires_at |
| 005 | Seed knowledge data |
| 006 | Deliverables tracking |
| 007 | Memory scoring: score, last_accessed_at |
| 008 | key_norm column + index for indexable dedup |

## Data Flow

1. Client sends POST /v1/chat/completions to proxy
2. Proxy computes session key from (system + first user message) hash
3. Proxy checks/updates session seed (frozen prefix)
4. Proxy assembles full context: seed + compactions + new messages
5. If context > max_tokens: trigger compaction via 4B worker
6. Proxy forwards to vLLM, streams response back to client
7. Proxy logs request, updates metrics, enqueues memory job
8. Worker picks up job, calls 4B model, applies memories to PostgreSQL
9. Dashboard polls all services, renders topology + metrics

## Key Design Decisions

- **Frozen prefix over sliding window**: Immutable prefix maximizes KV-cache
  reuse. Sliding windows would invalidate the cache on every request.
- **Token-weighted metrics**: Binary hit/miss is misleading. The real metric
  is what fraction of prompt tokens were cached.
- **key_norm for dedup**: Precomputed normalized key enables index-based
  lookup instead of full table scan. Also fixes a correctness bug where
  lower(raw_title) never matched _norm(title).
- **Atomic writes**: All file writes (status, config) use tmp+fsync+replace
  to prevent corruption on crash.
- **Environment-first config**: DSN, ports, tokens all overridable via env
  vars. config.yaml is the default, env vars take precedence.

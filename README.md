# ctxgate-proxy

**Token-aware context-gate proxy for long-running AI agents.**

Sits between your AI agent (Goose, or any OpenAI-compatible client) and a local vLLM inference server. Enforces deterministic context-window boundaries, injects durable memory from PostgreSQL, and streams responses back - with < 5 ms added latency on the hot path.

---

## Why ctxgate-proxy?

Long-running AI coding agents (Goose, Claude, etc.) accumulate context until the model's window overflows. Most solutions either:

- **Truncate blindly** (lose critical early context), or
- **Summarize in-context** (expensive, lossy, breaks KV-cache)

ctxgate-proxy takes a third approach:

| Problem | Solution |
|---|---|
| Context window overflow | Deterministic rolling-window trim (oldest middle messages first) |
| KV-cache invalidation | Frozen 3-message prefix (system + seed) is **never mutated** |
| Loss of durable knowledge | Async memory worker extracts facts to PostgreSQL; re-injected on demand |
| No observability | Live dashboard with health, metrics, and service control |
| Backend failures | Circuit breakers + fail-fast validation + systemd watchdog |

---

## Architecture at a Glance

```
  Agent (Goose / any OpenAI client)
       |
       |  POST /v1/chat/completions
       v
+---------------------------+
|   ctxgate-proxy :9201     |
|   (FastAPI, Python)       |
|                           |
|  - Token counting (Qwen)  |
|  - Rolling-window trim    |
|  - Frozen prefix guard    |
|  - Memory injection       |
|  - Event capture          |
+----+----------+-----------+
     |          |
     | main     | async (priority queue)
     | infer    |
     v          v
+-----------+  +------------------+
| vLLM      |  | Mistral API      |
| :29000    |  | (helper LM)      |
| Qwen3-27B |  | mistral-small    |
+-----------+  +--------+---------+
                           |
                           v
                    +-------------+
                    | PostgreSQL  |
                    | :5432       |
                    | ctxproxy DB |
                    +-------------+
                           ^
                           |
                    +------+------+
                    |  worker.py |  (separate process)
                    |  (flock,   |
                    |  SKIP LOCK)
                    +------------+

  Dashboard :9202 (health, metrics, service control)
```

**Two-model split:**
- **Main model** (vLLM, Qwen3.8-27B): handles all chat completions. Local, fast, full context.
- **Helper model** (Mistral API, mistral-small-latest): handles summaries, memory extraction, and knowledge tasks only. Never in the hot path.

---

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 15+ (local or remote)
- A vLLM server running a model (e.g., Qwen3.8-27B)
- (Optional) Mistral API key for the helper model
- (Optional) A Qwen tokenizer.json file for accurate token counting

### 1. Clone and configure

```bash
git clone https://github.com/your-org/ctxgate-proxy.git
cd ctxgate-proxy
cp .env.example .env
```

Edit `.env`:

```env
# Required
CTXGATE_DB_DSN=postgresql://postgres:your-password@127.0.0.1:5432/ctxproxy
CTXGATE_VLLM_URL=http://127.0.0.1:29000/v1
CTXGATE_VLLM_MODEL=Qwen3.8-27B
CTXGATE_QWEN_TOKENIZER=/path/to/tokenizer.json

# Helper LM (Mistral API)
CTXGATE_LM_URL=https://api.mistral.ai/v1
CTXGATE_LM_MODEL=mistral-small-latest
CTXGATE_LM_API_KEY=your-mistral-key

# Optional overrides
CTXGATE_MAX_CONTEXT=84000
CTXGATE_MAX_INPUT=64000
CTXGATE_SAFETY_MARGIN=2000
```

### 2. Create the database

```bash
createdb ctxproxy
psql ctxproxy < schema/001_init.sql
psql ctxproxy < schema/002_knowledge.sql
psql ctxproxy < schema/003_memory_worker.sql
psql ctxproxy < schema/004_memory_ttl.sql
psql ctxproxy < schema/006_deliverables.sql
psql ctxproxy < schema/007_memory_scoring.sql
psql ctxproxy < schema/008_memory_keynorm.sql
psql ctxproxy < schema/009_idx_memories_task_cat_key.sql
```

### 3. Install and run

```bash
pip install -r requirements.txt
python proxy/app.py
```

The proxy listens on **127.0.0.1:9201**. Point your agent's OpenAI base URL to it:

```
base_url: http://127.0.0.1:9201/v1
```

### 4. (Optional) Start the memory worker

```bash
python worker/worker.py
```

### 5. (Optional) Start the dashboard

```bash
python dashboard/dashboard.py
```

Open http://127.0.0.1:9202 for health monitoring and service control.

---

## Docker

```bash
docker compose up -d
```

See `.env.example` for all configurable values. The compose file starts PostgreSQL, the proxy, the worker, and the dashboard.

---

## How It Works

### Request Lifecycle

1. **Receive**: Agent sends `POST /v1/chat/completions` (OpenAI-compatible).
2. **Token count**: All messages are tokenized using the Qwen tokenizer (not byte estimates).
3. **Prefix guard**: The first 3 messages (system + seed user + seed assistant) are **frozen** per session. If they change, the session key is invalidated and KV-cache is reset.
4. **Trim**: If total input tokens exceed `MAX_INPUT` (default 64,000), oldest **middle** messages are dropped first. System prompt and newest user message are always preserved.
5. **Memory injection**: Durable memories from PostgreSQL (task-scoped) are appended to the **last user message** - never to the system prompt. A 60% overlap dedup gate prevents re-injecting facts already in context.
6. **Forward**: The assembled prompt is sent to vLLM. Streaming responses are passed through unchanged.
7. **Event capture**: The request is recorded in `proxy.events` (PostgreSQL) for the memory worker.
8. **Async jobs**: Trim summaries and knowledge extraction are enqueued to a priority queue (2 consumers, max 2 concurrent Mistral calls).

### Context Window Budget

| Parameter | Default | Purpose |
|---|---|---|
| `CTXGATE_MAX_CONTEXT` | 84,000 | Total model window |
| `CTXGATE_MAX_INPUT` | 64,000 | Max input tokens forwarded to vLLM |
| `CTXGATE_SAFETY_MARGIN` | 2,000 | Reserved for internal overhead |
| `CTXGATE_MAX_OUTPUT` | 18,000 | Reserved for model output |

Constraint enforced at startup: `MAX_INPUT + SAFETY_MARGIN <= MAX_CONTEXT`.

### Memory System

- **Extraction**: The worker process polls `proxy.memory_jobs` (FOR UPDATE SKIP LOCKED), calls the Mistral API with a strict JSON schema, and stores validated facts in `proxy.memories`.
- **Types**: DECISION, FINDING, FAILURE, TODO, CONSTRAINT, FILE, STATE, FACT
- **Actions**: NEW, UPDATE, SUPERSEDE, DUPLICATE, NO_CHANGE
- **TTL**: Non-critical, never-reused memories are pruned after 90 days (configurable).
- **Injection**: Memories are injected into the last user message on each request, scoped to the current task/session.
- **Working memory**: A single-line STATE/SUBTASK summary updated per event, injected first.

### Resilience

| Mechanism | Behavior |
|---|---|
| Circuit breakers | vLLM and LM breakers open after 5 consecutive failures, 30s cooldown, fail-fast when open |
| Config validation | `validate_config()` exits at startup if DSN is unparseable, tokenizer missing, or budget constraint violated |
| Systemd watchdog | 10s ping to NOTIFY_SOCKET; deadlocks detected at 60s |
| Worker single-instance | flock + heartbeat + stale takeover (30s TTL) |
| Outage backoff | Worker retries for up to 30 min before marking jobs failed |

---

## Configuration Reference

All settings are environment variables (see `.env.example`). Key variables:

| Variable | Default | Description |
|---|---|---|
| `CTXGATE_DB_DSN` | - | PostgreSQL connection string (required) |
| `CTXGATE_VLLM_URL` | - | vLLM server URL (required) |
| `CTXGATE_VLLM_MODEL` | - | Model name served by vLLM |
| `CTXGATE_QWEN_TOKENIZER` | - | Path to tokenizer.json (required for accurate counting) |
| `CTXGATE_LM_URL` | `https://api.mistral.ai/v1` | Helper LM base URL |
| `CTXGATE_LM_MODEL` | `mistral-small-latest` | Helper LM model name |
| `CTXGATE_LM_API_KEY` | - | Mistral API key |
| `CTXGATE_MAX_CONTEXT` | 84000 | Total context window tokens |
| `CTXGATE_MAX_INPUT` | 64000 | Max input tokens per request |
| `CTXGATE_SAFETY_MARGIN` | 2000 | Safety margin tokens |
| `CTXGATE_MAX_OUTPUT` | 18000 | Reserved output tokens |
| `CTXGATE_WORKER_POLL` | 2.0 | Worker poll interval (seconds) |
| `CTXGATE_WORKER_OUTAGE_TTL` | 1800 | Outage retry window (seconds) |
| `CTXGATE_MEMORY_TTL_DAYS` | 90 | Memory prune TTL (days) |

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | Main proxy endpoint (OpenAI-compatible) |
| `/health` | GET | Health check (vLLM, DB, worker status) |
| `/metrics` | GET | Request metrics, token counts, cache stats |
| `/sessions` | GET | Active session list with token usage |

---

## Project Structure

```
ctxgate-proxy/
+-- proxy/
|   +-- app.py              # Main FastAPI proxy (4100+ LOC)
|   +-- requirements.txt
+-- worker/
|   +-- worker.py           # Memory extraction worker (separate process)
+-- dashboard/
|   +-- dashboard.py        # Health monitoring GUI (:9202)
+-- schema/
|   +-- 001_init.sql        # Core tables (tasks, events, memories)
|   +-- 002_knowledge.sql   # Knowledge sharing tables
|   +-- 003_memory_worker.sql  # Memory job queue
|   +-- 004_memory_ttl.sql  # TTL pruning index
|   +-- 005_seed_knowledge.sql # Seed data
|   +-- 006_deliverables.sql # Deliverable tracking
|   +-- 007_memory_scoring.sql # Memory scoring
|   +-- 008_memory_keynorm.sql # Key normalization
|   +-- 009_idx_memories_task_cat_key.sql
+-- syncer/
|   +-- session_sync.py     # Session sync utility
+-- tests/                  # 17 test files
+-- .github/workflows/      # CI (test + CodeQL)
+-- Dockerfile
+-- docker-compose.yml
+-- Makefile
+-- start_proxy.sh          # Detached daemon launcher
+-- config.example.yaml
+-- .env.example
+-- README.md
+-- ARCHITECTURE.md
```

---

## Testing

```bash
make test
```

or

```bash
python -m pytest tests/ -v
```

17 test files cover: architecture invariants, dashboard, deliverables, e2e memory, hierarchical summaries, knowledge sharing, memory prefix, session isolation, streaming at cap, token budget, tool gauntlet, and unit/integration tests.

---

## Limitations

- **Single-node**: Designed for a single host. No distributed session state.
- **KV-cache assumption**: Frozen-prefix stability assumes the vLLM backend supports prefix caching. Without it, the proxy still works but with higher latency.
- **Helper LM is external**: The Mistral API call for summaries/memory adds 2-10s latency to async jobs (never to the hot path).
- **No auth**: The proxy has no authentication layer. Use a reverse proxy or firewall for network exposure.
- **Tokenizer dependency**: Accurate token counting requires the Qwen tokenizer.json file. Without it, falls back to a rough estimate.

---

## License

See [LICENSE](LICENSE).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

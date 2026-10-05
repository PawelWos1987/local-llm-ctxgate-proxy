# ctxgate-proxy

**Token-aware context gateway for long-running AI agents.**

A lightweight reverse proxy that sits between your AI agent (Goose, any OpenAI-compatible client) and a local vLLM inference server. It enforces deterministic context-window boundaries, injects durable memory from PostgreSQL, and streams responses back — with **< 5 ms** added latency on the hot path.

> **Bounded window, unbounded session.** Each request is capped at an 84 k-token model window, yet a single agent session can accumulate **15 M+ tokens** of cumulative throughput. → [Long-Session Capability](#long-session-capability)

---

## Why ctxgate-proxy?

Long-running AI agents accumulate context until the model's window overflows. Existing mitigations carry fundamental trade-offs:

| Approach | Trade-off |
|---|---|
| Blind truncation | Silently discards critical early context (decisions, constraints, file paths) |
| In-context summarization | Expensive, lossy, and invalidates the KV-cache on every turn |

ctxgate-proxy takes a third path that preserves both cache efficiency and knowledge continuity:

| Problem | How ctxgate-proxy solves it |
|---|---|
| Context overflow | Deterministic rolling-window trim — oldest *middle* messages first; system prompt and latest user message are always preserved |
| KV-cache invalidation | A frozen 3-message prefix (system + seed) is **never mutated**, keeping vLLM's prefix cache warm across turns |
| Loss of durable knowledge | An async memory worker extracts structured facts into PostgreSQL; relevant memories are re-injected on demand with a 60 % overlap-dedup gate |
| No observability | Live dashboard with health checks, token metrics, and service control |
| Backend failures | Circuit breakers, fail-fast startup validation, and a systemd watchdog |

---

## Long-Session Capability

The proxy's core design principle: **bound the per-request window, not the session.**

Every request forwarded to the model respects a strict, startup-validated token budget:

| Budget component | Tokens | Role |
|---|---:|---|
| `MAX_CONTEXT` — total model window | **84,000** | Hard limit of the inference model (e.g. Qwen3-27B) |
| `MAX_INPUT` — trim threshold | **64,000** | Maximum input tokens forwarded per request; oldest middle messages are trimmed when exceeded |
| `MAX_OUTPUT` — reasoning & output | **18,000** | Reserved for the model's chain-of-thought, tool calls, and response |
| `SAFETY_MARGIN` — overhead buffer | **2,000** | Covers internal framing, headers, and tokenizer edge-cases |
| **Startup constraint** | `64 000 + 2 000 ≤ 84 000` | The proxy refuses to start if this invariant is violated |

Because the proxy trims the oldest middle messages whenever input exceeds 64 k and re-injects durable memory from PostgreSQL, **the session itself has no upper bound**. The model never sees more than 84 k tokens in a single forward pass, but the cumulative token throughput of one session grows without limit.

### Measured session throughput

Accumulated token counts from a single long-running agent session (recorded in `proxy.events`):

| Cumulative total | Cumulative input | Cumulative output | Cache reads |
|---:|---:|---:|---:|
| 18 079 | 16 481 | 1 598 | 9 152 |
| 239 770 | 230 315 | 9 455 | 204 672 |
| 1 519 719 | 1 471 858 | 47 861 | 1 245 504 |
| 4 490 527 | 4 376 271 | 114 256 | 3 007 680 |
| 10 417 424 | 10 157 383 | 260 041 | 8 405 696 |
| **15 605 001** | **15 006 294** | **598 707** | **7 379 840** |

A single session processed **15.6 M tokens** of input while the model's per-request window never exceeded 84 k.

---

## Architecture

```
 Agent (Goose / any OpenAI-compatible client)
  |
  |  POST /v1/chat/completions
  v
+----------------------------+
|  ctxgate-proxy  :9201      |
|  (FastAPI, Python)         |
|                            |
|  • Token counting (Qwen)   |
|  • Rolling-window trim     |
|  • Frozen-prefix guard     |
|  • Memory injection        |
|  • Event capture           |
+-----+------------+---------+
      |            |
      | main       | async (priority queue, 2 consumers)
      | infer      |
      v            v
+-------------+  +-----------------+
| vLLM        |  | Mistral API     |
| :29000      |  | (helper LM)     |
| Qwen3-27B   |  | mistral-small   |
+-------------+  +--------+--------+
                         |
                         v
                  +--------------+
                  | PostgreSQL   |
                  | :5432        |
                  | ctxproxy DB  |
                  +--------------+
                         ^
                         |
                  +------+-------+
                  |  worker.py   |  (separate process)
                  |  flock +     |
                  |  SKIP LOCKED |
                  +--------------+

 Dashboard :9202  (health, metrics, service control)
```

**Two-model split:**

| Role | Model | When it runs |
|---|---|---|
| **Main** | vLLM / Qwen3-27B (local) | Every chat completion — the hot path |
| **Helper** | Mistral `mistral-small-latest` (API) | Summaries, memory extraction, knowledge tasks — async only, never in the hot path |

---

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 15+
- A vLLM server running a model with ≥ 84 k context (e.g. Qwen3-27B)
- *(Optional)* Mistral API key for the helper model
- *(Optional)* A Qwen `tokenizer.json` for accurate token counting

### 1. Clone & configure

```bash
git clone https://github.com/your-org/ctxgate-proxy.git
cd ctxgate-proxy
cp .env.example .env
```

Edit `.env`:

```env
# --- Required ---
CTXGATE_DB_DSN=postgresql://postgres:your-password@127.0.0.1:5432/ctxproxy
CTXGATE_VLLM_URL=http://127.0.0.1:29000/v1
CTXGATE_VLLM_MODEL=Qwen3-27B
CTXGATE_QWEN_TOKENIZER=/path/to/tokenizer.json

# --- Helper LM (Mistral API) ---
CTXGATE_LM_URL=https://api.mistral.ai/v1
CTXGATE_LM_MODEL=mistral-small-latest
CTXGATE_LM_API_KEY=your-mistral-key

# --- Token budget (defaults shown) ---
CTXGATE_MAX_CONTEXT=84000
CTXGATE_MAX_INPUT=64000
CTXGATE_MAX_OUTPUT=18000
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

### 3. Install & run

```bash
pip install -r requirements.txt
python proxy/app.py
```

The proxy listens on **`127.0.0.1:9201`**. Point your agent's OpenAI base URL to it:

```yaml
# goose config.yaml (example)
providers:
  - name: local
    type: openai
    base_url: http://127.0.0.1:9201/v1
    model: Qwen3-27B
```

### 4. (Optional) Memory worker

```bash
python worker/worker.py
```

### 5. (Optional) Dashboard

```bash
python dashboard/dashboard.py
# → http://127.0.0.1:9202
```

---

## Docker

```bash
docker compose up -d
```

The compose file starts PostgreSQL, the proxy, the worker, and the dashboard. See `.env.example` for all configurable values.

---

## How It Works

### Request lifecycle

1. **Receive** — Agent sends `POST /v1/chat/completions` (OpenAI-compatible).
2. **Token count** — All messages are tokenised with the Qwen tokenizer (not byte estimates).
3. **Prefix guard** — The first 3 messages (system + seed user + seed assistant) are **frozen** per session. If they change, the session key is invalidated and the KV-cache is reset.
4. **Trim** — If total input tokens exceed `MAX_INPUT` (64 k), oldest *middle* messages are dropped first. System prompt and newest user message are always preserved.
5. **Memory injection** — Durable memories from PostgreSQL (task-scoped) are appended to the **last user message** — never to the system prompt. A 60 % overlap-dedup gate prevents re-injecting facts already in context.
6. **Forward** — The assembled prompt is sent to vLLM. Streaming responses are passed through unchanged.
7. **Event capture** — The request is recorded in `proxy.events` for the memory worker.
8. **Async jobs** — Trim summaries and knowledge extraction are enqueued to a priority queue (2 consumers, max 2 concurrent Mistral calls).

### Memory system

| Aspect | Detail |
|---|---|
| **Extraction** | Worker polls `proxy.memory_jobs` (`FOR UPDATE SKIP LOCKED`), calls the helper LM with a strict JSON schema, stores validated facts in `proxy.memories` |
| **Fact types** | `DECISION`, `FINDING`, `FAILURE`, `TODO`, `CONSTRAINT`, `FILE`, `STATE`, `FACT` |
| **Actions** | `NEW`, `UPDATE`, `SUPERSEDE`, `DUPLICATE`, `NO_CHANGE` |
| **TTL** | Non-critical, never-reused memories are pruned after 90 days (configurable) |
| **Injection** | Memories are injected into the last user message, scoped to the current task/session |
| **Working memory** | A single-line `STATE` / `SUBTASK` summary updated per event, injected first |

### Resilience

| Mechanism | Behaviour |
|---|---|
| Circuit breakers | vLLM and LM breakers open after 5 consecutive failures; 30 s cooldown; fail-fast when open |
| Startup validation | `validate_config()` exits if DSN is unparseable, tokenizer missing, or budget constraint violated |
| Systemd watchdog | 10 s ping to `NOTIFY_SOCKET`; deadlocks detected at 60 s |
| Worker single-instance | `flock` + heartbeat + stale takeover (30 s TTL) |
| Outage backoff | Worker retries for up to 30 min before marking jobs failed |

---

## Configuration Reference

All settings are environment variables (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `CTXGATE_DB_DSN` | — | PostgreSQL connection string **(required)** |
| `CTXGATE_VLLM_URL` | — | vLLM server URL **(required)** |
| `CTXGATE_VLLM_MODEL` | — | Model name served by vLLM |
| `CTXGATE_QWEN_TOKENIZER` | — | Path to `tokenizer.json` **(required for accurate counting)** |
| `CTXGATE_LM_URL` | `https://api.mistral.ai/v1` | Helper LM base URL |
| `CTXGATE_LM_MODEL` | `mistral-small-latest` | Helper LM model name |
| `CTXGATE_LM_API_KEY` | — | Mistral API key |
| `CTXGATE_MAX_CONTEXT` | `84000` | Total context window tokens |
| `CTXGATE_MAX_INPUT` | `64000` | Max input tokens per request (trim threshold) |
| `CTXGATE_MAX_OUTPUT` | `18000` | Reserved for model reasoning & output |
| `CTXGATE_SAFETY_MARGIN` | `2000` | Overhead buffer |
| `CTXGATE_WORKER_POLL` | `2.0` | Worker poll interval (seconds) |
| `CTXGATE_WORKER_OUTAGE_TTL` | `1800` | Outage retry window (seconds) |
| `CTXGATE_MEMORY_TTL_DAYS` | `90` | Memory prune TTL (days) |

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | `POST` | Main proxy endpoint (OpenAI-compatible, streaming) |
| `/health` | `GET` | Health check (vLLM, DB, worker status) |
| `/metrics` | `GET` | Request metrics, token counts, cache stats |
| `/sessions` | `GET` | Active session list with token usage |

---

## Project Structure

```
ctxgate-proxy/
├── proxy/
│   ├── app.py                  # Main FastAPI proxy
│   └── requirements.txt
├── worker/
│   └── worker.py               # Memory extraction worker (separate process)
├── dashboard/
│   └── dashboard.py            # Health monitoring dashboard (:9202)
├── schema/
│   ├── 001_init.sql            # Core tables (tasks, events, memories)
│   ├── 002_knowledge.sql       # Knowledge sharing
│   ├── 003_memory_worker.sql   # Memory job queue
│   ├── 004_memory_ttl.sql      # TTL pruning
│   ├── 005_seed_knowledge.sql  # Seed data
│   ├── 006_deliverables.sql    # Deliverable tracking
│   ├── 007_memory_scoring.sql  # Memory scoring
│   ├── 008_memory_keynorm.sql  # Key normalization
│   └── 009_idx_memories_task_cat_key.sql
├── syncer/
│   └── session_sync.py         # Session sync utility
├── tests/                      # 17 test files
├── .github/workflows/          # CI (test + CodeQL)
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── .env.example
├── config.example.yaml
├── ARCHITECTURE.md
└── README.md
```

---

## Testing

```bash
make test
# or
python -m pytest tests/ -v
```

17 test files cover: architecture invariants, dashboard, deliverables, end-to-end memory, hierarchical summaries, knowledge sharing, memory prefix, session isolation, streaming at cap, token budget, tool gauntlet, and unit/integration tests.

---

## Limitations

- **Single-node** — Designed for a single host. No distributed session state.
- **KV-cache assumption** — Frozen-prefix stability assumes the vLLM backend supports prefix caching. Without it, the proxy still works but with higher latency.
- **Helper LM is external** — The Mistral API call for summaries/memory adds 2–10 s latency to *async* jobs only (never to the hot path).
- **No built-in auth** — The proxy has no authentication layer. Place a reverse proxy or firewall in front for network exposure.
- **Tokenizer dependency** — Accurate token counting requires the Qwen `tokenizer.json`. Without it, the proxy falls back to a rough estimate.

---

## License

See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.


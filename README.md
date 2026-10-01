# local-llm-ctxgate-proxy

> 🧠 **Token-aware rolling context proxy** for long-running AI coding agents.
> Sits between your agent (Goose) and a local LLM (vLLM), adding persistent memory,
> prefix-cache-safe injection, and async 4B memory extraction — **zero cloud dependency**.

[![CI](https://img.shields.io/github/actions/workflow/status/PawelWos1987/local-llm-ctxgate-proxy/ci.yml?branch=master&label=CI)](https://github.com/PawelWos1987/local-llm-ctxgate-proxy/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/PawelWos1987/local-llm-ctxgate-proxy/codeql.yml?branch=master&label=CodeQL)](https://github.com/PawelWos1987/local-llm-ctxgate-proxy/actions/workflows/codeql.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14%2B-336796?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/License-MIT-green?logo=gnu)](./LICENSE)
[![Binds](https://img.shields.io/badge/Binds-127.0.0.1-orange)](https://github.com/PawelWos1987/local-llm-ctxgate-proxy)

---

## 🏗️ Architecture

```mermaid
graph LR
    subgraph "Your Machine"
        G[🦢 Goose Agent] -->|HTTP :9200| P[⚡ ctxgate-proxy<br/>FastAPI]
        P -->|HTTP :29000| V[🧠 vLLM<br/>Qwen3.8-27B]
        P <-->|async jobs| PG[(🐘 PostgreSQL<br/>ctxproxy schema)]
        W[🔍 4B Worker<br/>qwen3-4b-instruct] -->|LM Studio :1234| L[💾 Memory Extract]
        W <--> PG
    end

    style G fill:#4a90d9,color:#fff
    style P fill:#e74c3c,color:#fff
    style V fill:#27ae60,color:#fff
    style PG fill:#336796,color:#fff
    style W fill:#8e44ad,color:#fff
    style L fill:#f39c12,color:#fff
```

### Request Lifecycle

```mermaid
sequenceDiagram
    participant G as 🦢 Goose
    participant P as ⚡ Proxy :9200
    participant PG as 🐘 PostgreSQL
    participant V as 🧠 vLLM 27B
    participant W as 🔍 4B Worker

    G->>P: POST /v1/chat/completions<br/>(X-Session-ID, messages)
    P->>PG: Resolve task + enqueue<br/>memory job (dedup + filter)
    P->>PG: Load working_memory<br/>+ relevant knowledge
    P->>V: Forward context-augmented<br/>messages (rolling window)
    V-->>P: SSE stream tokens
    P-->>G: Stream response

    Note over W,PG: Async (non-blocking)
    W->>PG: Claim pending job
    W->>W: 4B extracts memories<br/>+ updates working_memory
    W->>PG: INSERT memories<br/>UPDATE working_memory
```

---

## 📊 PostgreSQL Schema (6 tables)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `proxy.tasks` | One row per chat window (session) | `id`, `session_id`, `created_at`, `updated_at` |
| `proxy.events` | Ordered user/assistant messages per task | `id`, `task_id`, `seq`, `role`, `content` |
| `proxy.memory_jobs` | Async work queue for 4B worker | `id`, `task_id`, `event_id`, `status`, `attempts` |
| `proxy.memories` | Extracted durable facts (deduped) | `id`, `task_id`, `content`, `type`, `fingerprint` |
| `proxy.working_memory` | Current state snapshot per task | `task_id`, `content`, `updated_at` |
| `proxy.knowledge` | Cross-session shared knowledge | `id`, `content`, `tags`, `created_at` |

---

## 🚀 Quick Start

<details>
<summary><b>Prerequisites</b></summary>

- Python 3.10+
- PostgreSQL 14+ (local)
- vLLM serving a 27B model on `127.0.0.1:29000`
- LM Studio with a 4B model on `127.0.0.1:1234` (optional, for memory worker)
- Qwen tokenizer.json (for accurate token counting)

</details>

### 1. Install

```bash
git clone https://github.com/PawelWos1987/local-llm-ctxgate-proxy.git
cd local-llm-ctxgate-proxy
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set your DSN, model names, tokenizer path
```

### 3. Initialize Database

```bash
psql "$CTXGATE_DB_DSN" -f schema/001_init.sql
psql "$CTXGATE_DB_DSN" -f schema/002_knowledge.sql
psql "$CTXGATE_DB_DSN" -f schema/003_memory_worker.sql
```

### 4. Run

```bash
# Proxy (port 9200)
python proxy/app.py

# 4B Memory Worker (separate terminal)
python worker/worker.py
```

### 5. Point Goose at It

Set your LLM base URL to `http://127.0.0.1:9200/v1` in Goose config.

---

## 🛠️ Local Development

### Docker Compose (PostgreSQL 16)

A `docker-compose.yml` is provided for a local PostgreSQL 16 instance with the project schema auto-applied on first start:

```bash
make db        # starts postgres:16 with schema/001-003.sql auto-applied
make db-down   # stops the container
make db-logs   # tails PostgreSQL logs
```

The three schema files (`schema/001_init.sql`, `schema/002_knowledge.sql`, `schema/003_memory_worker.sql`) are mounted into `/docker-entrypoint-initdb.d/` so they run in order on the first container start.

### Makefile

| Target | Description |
|--------|-------------|
| `make test` | Run all tests (`pytest tests/ -v`) |
| `make test-unit` | Fast unit tests only (excludes e2e/stress/load) |
| `make lint` | Ruff + mypy |
| `make run` | Start the proxy (`python proxy/app.py`) |
| `make worker` | Start the memory worker (`python worker/worker.py`) |
| `make db` / `make db-down` / `make db-logs` | Docker Compose PostgreSQL |
| `make clean` | Remove `__pycache__`, `.mypy_cache`, `.pytest_cache`, `*.pyc` |

### Typical Dev Loop

```bash
# 1. Start the database
make db

# 2. Set environment variables (or source .env)
export CTXGATE_DB_DSN="postgresql://ctxproxy:ctxproxy@localhost:5432/ctxproxy"
export CTXGATE_VLLM_URL="http://127.0.0.1:29000/v1"
export CTXGATE_QWEN_TOKENIZER=" "

# 3. Run the fast test suite
make test-unit

# 4. Start the proxy
make run

# 5. In another terminal, start the memory worker
make worker

# 6. Point Goose at http://127.0.0.1:9200/v1
```

---

## ⚙️ Configuration

All settings via environment variables (see [**.env.example**](.env.example)):

| Variable | Default | Description |
|----------|---------|-------------|
| `CTXGATE_DB_DSN` | — | PostgreSQL connection string |
| `CTXGATE_VLLM_URL` | `http://127.0.0.1:29000/v1` | vLLM endpoint |
| `CTXGATE_VLLM_MODEL` | `Qwen3.8-27B` | Model name |
| `CTXGATE_LM_MODEL` | `qwen3-4b-instruct-2507` | 4B memory model |
| `CTXGATE_LM_URL` | `http://127.0.0.1:1234/v1/...` | LM Studio endpoint |
| `CTXGATE_QWEN_TOKENIZER` | — | Path to tokenizer.json |
| `CTXGATE_MEMORY_WORKER` | `1` | Set `0` to disable memory |
| `CTXGATE_WORKER_POLL` | `2.0` | Worker poll interval (seconds) |
| `CTXGATE_WORKER_MAX_ATTEMPTS` | `3` | Retry limit per job |
| `CTXGATE_WORKER_OUTAGE_TTL` | `1800` | Outage backoff window (seconds) |

---

## 🔌 API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/chat/completions` | POST | Main LLM proxy (OpenAI-compatible) |
| `/health` | GET | Liveness probe |
| `/ready` | GET | Readiness probe (PG + tokenizer) |
| `/metrics/prometheus` | GET | Prometheus metrics |
| `/dashboard` | GET | Basic HTML status page |
| `/api/sessions` | GET | List active sessions |
| `/api/memory` | GET | Query memories for a session |

---

## 🔒 Security

- 🔒 **Binds to 127.0.0.1 only** — never exposed to network
- 🔒 **No cloud dependency** — all data stays local
- 🔒 **`.env` gitignored** — secrets never committed
- 🔒 **GitHub Ruleset** — CI + CodeQL enforced on all pushes
- 🔒 **Single-user** — one developer, admin-only bypass
- 🔒 **Single-instance worker** — the memory worker self-locks; a second start exits cleanly, and a frozen instance is auto-replaced

<details>
<summary><b>Ruleset: protect master</b></summary>

| Rule | Status |
|------|--------|
| Require status checks (ci + CodeQL) | ✅ ON |
| Do not enforce on creation | ✅ ON |
| Code scanning (CodeQL, high+) | ✅ ON |
| All other 11 rules | ❌ OFF |
| Bypass | PawelWos1987 (always) |

</details>

---

## 📁 Project Structure

```
local-llm-ctxgate-proxy/
├── proxy/
│   ├── app.py              # FastAPI proxy (main)
│   └── requirements.txt
├── worker/
│   └── worker.py           # 4B memory worker
├── schema/
│   ├── 001_init.sql        # Core tables
│   ├── 002_knowledge.sql   # Knowledge sharing
│   └── 003_memory_worker.sql  # Memory jobs + WM
├── tests/                  # 20+ test files
├── benchmarks/             # Performance benchmarks
├── .github/
│   ├── workflows/
│   │   ├── ci.yml         # CI pipeline
│   │   └── codeql.yml     # Security scanning
│   └── dependabot.yml
├── .env.example
├── .gitignore
├── ARCHITECTURE.md
├── SECURITY.md
├── LICENSE
└── README.md
```

---

## 📈 Performance Characteristics

| Metric | Target |
|--------|--------|
| Proxy add. latency | < 5 ms (token count + PG lookup) |
| 4B memory extraction | ~200 ms per job (async, non-blocking) |
| Token counting | Exact (Qwen tokenizer, not estimation) |
| Context window | 84k tokens (vLLM) / 64k input cap |
| Concurrent sessions | Unlimited (per-session isolation) |

---

## 📜 License

[MIT](./LICENSE) — do whatever, no warranty.

---

<p align="center">
  <b>local-llm-ctxgate-proxy</b> · Built for local-first AI agents · No cloud · No tracking · No telemetry
</p>

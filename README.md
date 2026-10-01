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

## 🧪 Tested Stack & Benchmark Results

This project was developed and stress-tested on the local stack below. Headline result: a **single 27B model on 2× consumer 16 GB GPUs sustained an 8,000,000-token session** with zero errors and zero prefix-cache invalidations — the rolling-window proxy kept live context at ~59k tokens, far under the 84k vLLM window.

### Hardware

| Component | Spec |
|-----------|------|
| GPU | 2× NVIDIA GeForce RTX 5070 Ti (16 GB each) |
| GPU driver | NVIDIA 615.71.09 (CUDA UMD 13.4) |
| Arch | x86_64 |
| OS | CachyOS Linux (rolling, Arch-based), kernel 7.2.8-1-cachyos |

### Software

| Component | Version | Role |
|-----------|---------|------|
| vLLM | 0.30.0 | Serves the 27B model (TP=2) on :29000 |
| 27B model | Swift-1.5-Qwen3.8-27b (W4A16 AutoRound) | Main LLM, served as `Qwen3.8-27B` |
| Speculative draft (vLLM) | incoai/Qwen3.8-27B-DFlash2 (dflash, 3 tokens) | vLLM speculative decoding |
| LM Studio | 0.4.25 | Serves the 4B memory model on :1234 |
| llama.cpp backend | 2.49.0 (linux-x86_64-avx2) | LM Studio inference engine |
| 4B model | Qwen3-4B-Instruct-2507 (UD Q6_K_XL GGUF) | Async memory extractor |
| 4B draft model | Qwen3-Coder-Instruct-DRAFT-0.75B (Q4_0 GGUF) | LM Studio speculative decoding |
| PostgreSQL | 16 (Docker) | Memory / knowledge store |
| Python | 3.10+ | Proxy + worker |
| FastAPI | 0.100+ | Proxy framework |

### Disk / memory footprint

| Item | Size |
|------|------|
| 27B model (W4A16 AutoRound, on disk) | ~19 GB |
| 4B model (Q6_K_XL GGUF, on disk) | ~3.5 GB |
| 4B draft (Q4_0 GGUF, on disk) | ~448 MB |
| vLLM GPU memory (2× RTX 5070 Ti) | ~15.9 GiB / GPU (≈31.8 GiB total) |
| vLLM KV cache (FP8, reserved) | 1.88 GiB / GPU → 84,536 tokens |

### vLLM run command (27B, TP=2, speculative decoding)

```bash
vllm serve /home/user/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound \
  --served-model-name Qwen3.8-27B \
  --tensor-parallel-size 2 \
  --disable-custom-all-reduce \
  --max-model-len 84000 \
  --max-num-batched-tokens 4992 \
  --max-num-seqs 1 \
  --dtype bfloat16 \
  --kv-cache-dtype fp8 \
  --kv-cache-memory-bytes 1970000K \
  --mamba-ssm-cache-dtype bfloat16 \
  --mamba-cache-mode align \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --enable-prompt-tokens-details \
  --language-model-only \
  --quantization auto-round \
  --attention-backend FLASHINFER \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --performance-mode interactivity \
  --generation-config vllm \
  --max-log-len 0 \
  --mm-processor-cache-gb 0 \
  --limit-mm-per-prompt.image 0 \
  --limit-mm-per-prompt.video 0 \
  --limit-mm-per-prompt.audio 0 \
  --trust-remote-code \
  --default-chat-template-kwargs '{"enable_thinking": true, "preserve_thinking": false, "reasoning_effort": "medium"}' \
  --speculative-config '{"method": "dflash","model": "incoai/Qwen3.8-27B-DFlash2","num_speculative_tokens": 3}' \
  --port 29000
```

### LM Studio run command (4B memory model, CPU, speculative decoding)

Served by LM Studio 0.4.25 (llama.cpp 2.49.0). Launched on 127.0.0.1 (LM Studio exposes it at :1234):

```bash
llama-server \
  --model /home/user/.lmstudio/models/unsloth/Qwen3-4B-Instruct-2507-GGUF/Qwen3-4B-Instruct-2507-UD-Q6_K_XL.gguf \
  --host 127.0.0.1 \
  --port 37575 \
  --api-key <your-api-key> \
  --no-webui \
  --jinja \
  --ctx-size 8192 \
  --n-gpu-layers 0 \
  --threads 16 \
  --parallel 1 \
  --batch-size 2048 \
  --ubatch-size 512 \
  --ctx-checkpoints 8 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --flash-attn on \
  --no-kv-offload \
  --kv-unified \
  --load-mode mmap+mlock \
  --spec-type draft-simple \
  --spec-draft-model /home/user/.lmstudio/models/jukofyork/Qwen3-Coder-Instruct-DRAFT-0.75B-GGUF/Qwen3-Coder-Instruct-DRAFT-0.75B-32k-Q4_0.gguf \
  --spec-draft-n-max 3 \
  --spec-draft-n-min 3 \
  --spec-draft-p-min 0
```

### Throughput (measured, vLLM 27B)

| Metric | Value |
|--------|-------|
| Generation throughput (median) | **~110–120 tokens/s** (observed range 90–136 tok/s) |
| Prefill (prompt) throughput | 270–1300 tokens/s |
| Speculative decoding | mean acceptance length ~2.7–3.4; draft acceptance 57–79% |
| Prefix-cache hit rate | ~73% |

### 8M-token long-run test — PASSED (2026-10-01)

`tests/test_8m_longrun.py` — session `8m-longrun-001`, target 8,000,000 cumulative input tokens:

| Metric | Value |
|--------|-------|
| Total input tokens | **8,027,779** ✅ (target 8,000,000) |
| Total output tokens | 5,714 |
| Total requests | 139 (build=9, rapid=121) |
| Prefix invalidations | **0** ✅ |
| Error requests | **0** ✅ |
| Latency avg / min / max | 6.53s / 1.03s / 52.35s |
| Proxy memory | 24.4 MB → 25.3 MB (+0.9 MB, stable) |
| Final live context | ~59,043 tokens / 261 messages |
| Peak context | 64,524 tokens (≈19.5k below the 84k vLLM window) |

**Takeaway:** a 27B model on two consumer 16 GB GPUs, run through this proxy, can carry a multi-million-token working session. The rolling-window trim + prefix-cache-safe injection keeps the live window pinned near 64k while cumulative input grows without bound — no OOM, no context overflow, no dropped requests.

---

## 📜 License

[MIT](./LICENSE) — do whatever, no warranty.

---

<p align="center">
  <b>local-llm-ctxgate-proxy</b> · Built for local-first AI agents · No cloud · No tracking · No telemetry
</p>

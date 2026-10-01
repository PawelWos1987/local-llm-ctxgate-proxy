# local-llm-ctxgate-proxy

**A lightweight local context proxy for long-running AI coding agents.**

local-llm-ctxgate-proxy is a thin intelligent layer between your AI agent (e.g. [Goose](https://github.com/aaif/goose)) and a local model stack (e.g. [vLLM](https://github.com/vllm-project/vllm) + [LM Studio](https://lmstudio.ai/)). It provides token-aware rolling context, persistent PostgreSQL memory, and asynchronous small-model memory extraction — so your agent can run for hours without losing its place.

## How it works

```
Goose (agent)
   |
   |  http://127.0.0.1:9200/v1
   v
+---------------------------+
|  local-llm-ctxgate-proxy (FastAPI)       |
|  - Token-aware context    |
|    assembly + trimming    |
|  - Conditional memory     |
|    injection (deduped)    |
|  - SSE streaming proxy    |
|  - Session isolation      |
+------------+--------------+
             |  http://127.0.0.1:29000/v1
             v
        vLLM (27B model)

         +---------------------+
         |  4B Memory Worker   |<-- polls memory_jobs (async)
         |  (LM Studio)        |--> PostgreSQL
         +---------------------+
```

**The three mechanisms, cleanly separated:**

| Layer | Responsibility |
|-------|---------------|
| **Goose compaction** | Short-term continuity — in-context summarization at 65% of window |
| **local-llm-ctxgate-proxy** | Context boundary + small conditional memory supplement (deduped, relevance-gated, ≤2k tokens) |
| **4B worker** | Async durable-memory extraction — never blocks the main model |

## Features

- **Token-aware context assembly** — preserves system prompt, first user message, and most recent turns; trims the middle
- **Conditional memory injection** — dedupes against current context (60% token overlap), gates on deterministic relevance, budgets ≤2k tokens total. Zero hot-path overhead when context is sufficient.
- **Persistent PostgreSQL memory** — decisions, findings, failures, constraints, files, state. Survives compaction and session restarts.
- **Asynchronous 4B memory worker** — Qwen3-4B (LM Studio) extracts durable memories from events. Fully async: the 27B model never waits. Outage-aware with TTL-based backoff.
- **SSE streaming** — full streaming support with usage tokens
- **Session isolation** — per-session task/memory scoping via `X-Session-ID`
- **Tool call forwarding** — transparently passes `tools` and `tool_choice` with sanitization
- **Prefix-cache safe** — memory injection at end of system prompt preserves vLLM prefix cache
- **Health & metrics** — `/health` and `/metrics` endpoints

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- vLLM or LM Studio serving a 27B model (e.g. Qwen3.8-27B)
- (Optional) LM Studio serving a 4B model (e.g. Qwen3-4B) for the memory worker

### 1. Install

```bash
git clone https://github.com/PawelWos1987/local-llm-ctxgate-proxy.git
cd local-llm-ctxgate-proxy
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your PostgreSQL DSN and model URLs
```

### 3. Initialize database

```sql
-- Run in order:
psql -U postgres -d local-llm-ctxgate-proxy -f schema/001_init.sql
psql -U postgres -d local-llm-ctxgate-proxy -f schema/002_knowledge.sql
psql -U postgres -f schema/003_memory_worker.sql
```

### 4. Start the proxy

```bash
uvicorn proxy.app:app --host 127.0.0.1 --port 9200
```

### 5. (Optional) Start the 4B memory worker

```bash
python3 worker/worker.py
```

### 6. Point your agent at the proxy

Configure your agent's provider to use `http://127.0.0.1:9200/v1` as the base URL.

## Security

local-llm-ctxgate-proxy is designed for **local, single-user** use. It binds to `127.0.0.1` by default. See [SECURITY.md](SECURITY.md) for the full security policy and threat model.

## Testing

```bash
# Architecture tests (deterministic, 11 checks)
python3 -B tests/test_architecture.py

# Performance A/B/C comparison
python3 tests/test_perf_abc.py

# 8M-token long-run test
python3 tests/test_8m_longrun.py
```

## License

[MIT](LICENSE)

# local-llm-ctxgate-proxy

> A **token-aware rolling context proxy** for long-running AI coding agents.
> Sits between Goose and a local LLM (vLLM), adding persistent memory,
> prefix-cache-safe injection, and async 4B memory extraction — **zero cloud dependency**.

---

## What It Does

```
Goose Desktop ──► ctxgate-proxy:9200 ──► vLLM:29000 (Qwen3.8-27B)
                    │
                    ├── PostgreSQL (memory, knowledge, events)
                    ├── 4B Worker (async fact extraction)
                    └── LM Studio:1234 (qwen3-4b-instruct-2507)
```

| Feature | How |
|---|---|
| **Context window enforcement** | Token-aware trim to 64k input (Qwen tokenizer) |
| **Prefix-cache safety** | Memory injected at END of system prompt; prefix fingerprint tracked |
| **Conditional memory injection** | Only injects facts NOT already in context (60% overlap gate) |
| **Cross-session knowledge** | Global knowledge table, 4B-extracted, quality-gated (2-pass) |
| **Auto-continuation** | Transparently continues generation when vLLM hits max_tokens (up to 5x) |
| **Reasoning visibility** | reasoning_content forwarded to Goose Desktop (stream + non-stream) |
| **APC cache display** | prompt_tokens_details.cached_tokens always present in response usage |
| **Async memory extraction** | Separate 4B worker process — zero impact on request latency |
| **Per-session isolation** | Each Goose session gets its own task, memories, and working state |

---

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 15+ (local)
- vLLM with Qwen3.8-27B (2x TP, --enable-prefix-caching --enable-prompt-tokens-details)
- LM Studio with qwen3-4b-instruct-2507 (port 1234)
- Goose configured with base_url: http://127.0.0.1:9200/v1

### Setup

```bash
cd /home/user/ctxproxy
pip install -r proxy/requirements.txt

# Initialize database schema
psql $CTXGATE_DB_DSN -f schema/001_init.sql
psql $CTXGATE_DB_DSN -f schema/002_knowledge.sql
psql $CTXGATE_DB_DSN -f schema/003_memory_worker.sql
psql $CTXGATE_DB_DSN -f schema/004_memory_ttl.sql

# Configure
cp .env.example .env  # then edit values
```

### Run

```bash
# Start proxy (with supervisor - recommended)
setsid bash supervisor.sh &

# Start 4B memory worker
setsid python3 -u worker/worker.py >> worker.log 2>&1 &

# Verify
curl http://127.0.0.1:9200/health
curl http://127.0.0.1:9200/ready
```

### Stop

```bash
pkill -f 'supervisor.sh'
pkill -f 'proxy/app.py'
pkill -f 'worker/worker.py'
```

---

## Configuration

All settings via environment variables (.env file):

| Variable | Default | Description |
|---|---|---|
| CTXGATE_MAX_CONTEXT | 84000 | Total model context window |
| CTXGATE_MAX_INPUT | 64000 | Max input tokens (trim threshold) |
| CTXGATE_MAX_OUTPUT | 18000 | Max output tokens per generation |
| CTXGATE_SAFETY_MARGIN | 2000 | Reserved tokens (safety buffer) |
| CTXGATE_VLLM_URL | http://127.0.0.1:29000/v1 | vLLM endpoint |
| CTXGATE_VLLM_MODEL | Qwen3.8-27B | Model name |
| CTXGATE_LM_URL | http://127.0.0.1:1234/v1 | 4B model endpoint |
| CTXGATE_LM_MODEL | qwen3-4b-instruct-2507 | 4B model name |
| CTXGATE_QWEN_TOKENIZER | — | Path to tokenizer.json |
| CTXGATE_DB_DSN | — | PostgreSQL connection string |
| CTXGATE_PROXY_PORT | 9200 | Listen port |
| CTXGATE_MAX_CONTINUATIONS | 5 | Auto-continuation max retries |
| CTXGATE_WALL_CLOCK_MAX | 1800 | Max seconds per request |
| CTXGATE_MEMORY_WORKER | 1 | Enable memory job enqueue |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full variable reference.

---

## Architecture

Three-layer memory separation:

| Layer | Timescale | Mechanism |
|---|---|---|
| Goose auto-compact | Per-session (backstop at 90%) | In-context summarization |
| ctxgate-proxy | Per-request (deterministic) | Rolling window trim + conditional injection |
| 4B memory worker | Per-turn (async) | Durable fact extraction to PostgreSQL |

The proxy is the **primary** context manager. Goose compaction is a **safety gate** that only fires if the proxy's trim is insufficient. The 4B worker supplies durable facts so the agent can recover what was trimmed.

Full details: [ARCHITECTURE.md](ARCHITECTURE.md)

---

## API

| Endpoint | Purpose |
|---|---|
| POST /v1/chat/completions | Main proxy (OpenAI-compatible) |
| GET /health | Liveness |
| GET /ready | Readiness (DB + vLLM) |
| GET /metrics | JSON metrics |
| GET /metrics/prometheus | Prometheus format |
| GET /api/sessions | Active sessions |
| GET /api/recent-calls | Recent call history |
| GET /api/memory | Memory entries |
| GET /api/memory-analytics | Injection analytics |
| GET /knowledge/search | Search knowledge |
| POST /knowledge | Create knowledge item |
| GET /dashboard | HTML dashboard |

Full route reference: [ARCHITECTURE.md](ARCHITECTURE.md)

---

## vLLM Requirements

The proxy expects vLLM started with:

```bash
vllm serve /path/to/model \
  --served-model-name Qwen3.8-27B \
  --tensor-parallel-size 2 \
  --max-model-len 84000 \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --enable-chunked-prefill \
  --kv-cache-dtype fp8 \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --speculative-config '{"method": "dflash", "model": "incoai/Qwen3.8-27B-DFlash2", "num_speculative_tokens": 3}' \
  --performance-mode interactivity \
  --max-num-seqs 1 \
  --port 29000
```

Key flags: --enable-prefix-caching (APC), --enable-prompt-tokens-details (cache visibility in Goose Desktop), --reasoning-parser qwen3 (thinking visibility).

---

## Project Structure

```
ctxproxy/
+-- proxy/app.py          # 3,046 LOC - FastAPI proxy (main)
+-- worker/worker.py      # 708 LOC - 4B memory worker
+-- schema/               # PostgreSQL migrations (5 files)
+-- dashboard/            # HTML dashboard
+-- tests/                # 28 test files
+-- supervisor.sh         # Process manager (lock + backoff)
+-- .env                  # Configuration
+-- ARCHITECTURE.md       # Full technical reference
+-- README.md             # This file
```

---

## License

MIT

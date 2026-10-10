# ctxgate — Rolling-Window Context Proxy

A local-first HTTP proxy that sits between autonomous AI coding agents (e.g., [Goose](https://github.com/aaif/goose)) and a [vLLM](https://github.com/vllm-project/vllm) inference server. It manages rolling context windows, persistent memory, and session identity for long-running agent sessions — enabling multi-hour autonomous coding sessions without hitting model context limits.

**This is a local-first project, not a hardened multi-tenant service.**

---

## What It Solves

Autonomous coding agents (Goose, Claude Code, Cursor, etc.) generate long conversations that eventually exceed a model's context window. When this happens, the agent loses track of prior decisions, file states, and task context — derailing multi-hour workflows.

ctxgate solves this by:

1. **Sliding the context window** — When the conversation grows beyond the token budget, older messages are summarized and compressed, preserving the most recent exchanges and critical facts.
2. **Optimizing KV-cache reuse** — A frozen message prefix stays stable across requests, allowing vLLM's prefix cache to avoid re-computing attention for unchanged context.
3. **Persisting memory** — A background worker extracts durable facts (decisions, file states, constraints) from conversations and injects them into future sessions, so knowledge survives context resets.
4. **Maintaining session identity** — Sessions are tracked across requests so the proxy knows which conversation a request belongs to, enabling per-session context management.

---

## Current Status

| Aspect | Status |
|--------|--------|
| Core proxy (rolling window, streaming, sessions) | **Operational** — running in production on the developer's machine |
| Memory worker (extraction, injection) | **Operational** — 24,230+ requests processed, 34,421 memory injections recorded |
| Dashboard (monitoring, config editing) | **Operational** |
| PostgreSQL persistence | **Operational** — 13 schema migrations applied |
| systemd integration | **Operational** — Type=notify, watchdog, auto-restart |
| Multi-tenant API keys | **Implemented** — per-tenant rate limiting and concurrency caps |
| Prometheus metrics | **Implemented** — /metrics/prometheus endpoint |
| CI/CD | **Minimal** — manual-trigger workflow, single test file |
| License | **Not selected** |
| Production hardening | **Out of scope** — designed for single-user local use |

---

## Architecture

```mermaid
graph TB
    Agent[AI Coding Agent<br/>e.g. Goose] -->|OpenAI-compatible<br/>HTTP + SSE| Proxy

    subgraph ctxgate-proxy
        Proxy[FastAPI Proxy<br/>proxy/app.py<br/>6,359 LOC]
        Proxy -->|Token counting| Tokenizer[Qwen Tokenizer<br/>tokenizer.json]
        Proxy -->|Read/Write| PG[(PostgreSQL 16<br/>13 migrations)]
        Proxy -->|Summarization<br/>requests| LM[External LM<br/>Mistral AI]
    end

    Proxy -->|Context-managed<br/>requests| VLLM[vLLM Server<br/>Qwen3.8-27B<br/>84K context]

    subgraph ctxgate-worker
        Worker[Memory Worker<br/>worker/worker.py<br/>1,272 LOC]
        Worker -->|Poll job queue| PG
        Worker -->|Extraction<br/>requests| LM
    end

    subgraph ctxgate-dashboard
        Dash[Dashboard<br/>dashboard/dashboard.py<br/>2,432 LOC]
        Dash -->|Health, metrics| Proxy
        Dash -->|Config, logs| PG
    end
```

### Components

| Component | File | Lines | Role |
|-----------|------|-------|------|
| **Proxy** | `proxy/app.py` | 6,359 | FastAPI server. Receives OpenAI-compatible requests, manages context windows, streams responses, tracks sessions. |
| **Worker** | `worker/worker.py` | 1,272 | Background process. Polls PostgreSQL job queue, calls external LM (Mistral) to extract and summarize memory, writes results back. |
| **Dashboard** | `dashboard/dashboard.py` | 2,432 | Web UI for monitoring health, viewing metrics, editing configuration, and managing services. |
| **Schema** | `schema/*.sql` | 13 files | PostgreSQL migrations. Applied idempotently at startup. |

### Request Flow

1. Agent sends `POST /v1/chat/completions` (OpenAI-compatible format).
2. Proxy authenticates (API key or no-auth for loopback).
3. Proxy resolves session identity (Goose SQLite lookup or deterministic hash).
4. Proxy counts tokens in the full conversation using the Qwen tokenizer.
5. If over budget: proxy trims older messages (two-path algorithm: sticky prefix reuse or full re-cut), requesting summaries from the external LM.
6. Proxy injects relevant memory (task memory, knowledge, working memory) as a system-message block.
7. Proxy forwards the managed context to vLLM.
8. For streaming: proxy passes through SSE chunks with heartbeat keepalive.
9. After completion: proxy enqueues a memory-extraction job for the worker.

---

## Verified Capabilities

The following capabilities are confirmed by source code inspection and/or live system observation:

| Capability | Evidence |
|------------|----------|
| OpenAI-compatible `/v1/chat/completions` | Source: proxy/app.py:3757 |
| SSE streaming with heartbeat | Source: proxy/app.py streaming path |
| Rolling context window (token-budgeted) | Source: two-path trim algorithm |
| Frozen prefix for KV-cache optimization | Source: 3-message frozen seed |
| Qwen tokenizer for accurate token counting | Source: CTXGATE_QWEN_TOKENIZER (required) |
| Session identity (Goose + hash modes) | Source: CTXGATE_SESSION_IDENTITY |
| Multi-tenant API keys with rate limiting | Source: CTXGATE_API_KEYS, per-tenant rate/concurrency |
| Memory worker (task, knowledge, working memory) | Source: worker/worker.py, 3 injection types |
| PostgreSQL persistence (13 migrations) | Source: schema/*.sql |
| systemd Type=notify with watchdog | Source: deploy/ctxgate-proxy.service |
| Config validation (`--check-config`, exit 78) | Source: both proxy and worker |
| Prometheus metrics endpoint | Source: /metrics/prometheus |
| Dashboard with real-time monitoring | Source: dashboard/dashboard.py (14 endpoints) |
| External LM integration (Mistral AI) | Source: CTXGATE_LM_* env vars |
| Graceful shutdown with in-flight drain | Source: signal handlers, sd_notify |
| FD hygiene auto-restart | Source: CLOSE_WAIT monitoring |
| Tool call passthrough (OpenAI format) | Source: streaming tool_calls delta handling |
| Reasoning/thinking token passthrough | Source: reasoning_content delta handling |

---

## Test Environment

All benchmarks and operational data in this repository were collected in the following environment:

| Component | Specification | Source |
|-----------|--------------|--------|
| GPU | NVIDIA GeForce RTX 5070 Ti, 16 GB GDDR7 | nvidia-smi, 2026-10-01 |
| GPU Driver | NVIDIA 615.71.09 | nvidia-smi |
| CUDA | 13.4 | nvidia-smi |
| Model | Qwen3.8-27B (W4A16-AutoRound quantization) | vLLM /v1/models |
| Max Context | 84,000 tokens | vLLM max_model_len |
| Inference Server | vLLM 0.30 | _local/notes/PHASE0_BENCH_VLLM.txt |
| External LM | Mistral AI (mistral-small-latest) | .env |
| Python | 3.12 | pyproject.toml |
| Database | PostgreSQL 16 | docker-compose.yml |
| OS | Linux (Ubuntu-based) | systemd units |
| Proxy Port | 9201 | config |
| vLLM Port | 29000 | .env |

> **Note:** This is the developer's single-machine environment. The project has not been tested on other hardware configurations, GPU models, or operating systems.

---

## Prerequisites

| Requirement | Minimum | Tested With |
|-------------|---------|-------------|
| GPU | NVIDIA with >=16 GB VRAM | 2x RTX 5070 Ti (16 GB each) |
| CUDA | >=12.0 | 13.4 |
| GPU Driver | >=550 | 615.71.09 |
| vLLM | >=0.30 | 0.30.0 |
| Model | Qwen3.8-27B (W4A16) or compatible 27B-class | Swift-1.5-Qwen3.8-27b-W4A16-AutoRound |
| Python | >=3.10 | 3.12 |
| PostgreSQL | >=16 | 16 (Docker) |
| OS | Linux with systemd | Ubuntu-based |
| External LM API key | Mistral AI (optional) | mistral-small-latest |

> The proxy **requires** a Qwen tokenizer file (tokenizer.json) for accurate token counting. There is no fallback — the proxy will exit with code 78 if the tokenizer path is missing or unreadable.

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/PawelWos1987/local-llm-ctxgate-proxy.git
cd local-llm-ctxgate-proxy
```

### 2. Set Up the Python Environment

```bash
python3.12 -m venv /opt/ctxgate-proxy/.venv
/opt/ctxgate-proxy/.venv/bin/pip install -e ".[dev]"
```

### 3. Configure PostgreSQL

Option A — Docker (simplest):

```bash
docker compose up -d
```

This starts PostgreSQL 16 on port 5432. Migrations 001–007 are applied automatically by the Docker entrypoint. Apply the remaining migrations (008–013) manually:

```bash
psql "$CTXGATE_DB_DSN" -f schema/008_memory_keynorm.sql
psql "$CTXGATE_DB_DSN" -f schema/009_idx_memories_task_cat_key.sql
psql "$CTXGATE_DB_DSN" -f schema/010_deliverables_unique.sql
psql "$CTXGATE_DB_DSN" -f schema/011_phase_summaries.sql
psql "$CTXGATE_DB_DSN" -f schema/012_session_windows.sql
psql "$CTXGATE_DB_DSN" -f schema/013_memory_hardening.sql
```

> **Note:** Migration 012 is currently gitignored and will not be present in fresh clones. It must be obtained from the developer's environment or recreated.

Option B — Existing PostgreSQL instance:

Create a database and user, then apply all 13 migration files in order.

### 4. Create the Environment File

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

| Variable | Example | Notes |
|----------|---------|-------|
| CTXGATE_DB_DSN | postgresql://ctxgate:yourpassword@127.0.0.1:5432/ctxproxy | Must not contain CHANGE_ME |
| CTXGATE_QWEN_TOKENIZER | /path/to/tokenizer.json | Path to Qwen tokenizer file |
| CTXGATE_VLLM_URL | http://127.0.0.1:29000/v1 | Your vLLM server address |
| CTXGATE_VLLM_MODEL | Qwen3.8-27B | Must match your vLLM model name |

### 5. Validate Configuration

```bash
/opt/ctxgate-proxy/.venv/bin/python proxy/app.py --check-config
/opt/ctxgate-proxy/.venv/bin/python worker/worker.py --check-config
```

Both commands exit 0 on success, 78 on configuration error.

### 6. Start the Services

For development (foreground):

```bash
# Terminal 1: proxy
CTXGATE_ALLOW_NO_AUTH=1 CTXGATE_HOST=127.0.0.1 /opt/ctxgate-proxy/.venv/bin/python -u proxy/app.py

# Terminal 2: worker
/opt/ctxgate-proxy/.venv/bin/python -u worker/worker.py
```

For production (systemd):

```bash
make install-systemd
systemctl --user start ctxgate-proxy
systemctl --user start ctxgate-worker
```

> **Note:** The `make install-systemd` target references `deploy/ctxgate-dashboard.service` which is not present in the repository. The dashboard service unit must be created separately if needed.

---

## Configuration Reference

All configuration is environment-variable driven. The `config.yaml` file in the repository root is a legacy artifact and is **not read** by the proxy.

### Required

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_DB_DSN | string | — | PostgreSQL connection string. Must not contain CHANGE_ME. |
| CTXGATE_QWEN_TOKENIZER | string | — | Path to Qwen tokenizer.json. Required, no fallback. |
| CTXGATE_VLLM_URL | string | http://127.0.0.1:29000/v1 | Upstream vLLM server base URL. |
| CTXGATE_VLLM_MODEL | string | Qwen3.8-27B | Model name forwarded to vLLM. |

### Context Window

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_MAX_CONTEXT | int | 84000 | Total context window token budget. |
| CTXGATE_MAX_INPUT | int | 58000 | Maximum input tokens after trimming. |
| CTXGATE_SAFETY_MARGIN | int | 3500 | Token safety margin reserved for system messages. |
| CTXGATE_MIN_OUTPUT | int | 16000 | Hard floor for output token budget. |
| CTXGATE_TRIM_TARGET_FRACTION | float | 0.70 | Fraction of MAX_INPUT used as trim target. |
| CTXGATE_TRIM_TARGET_FLOOR | int | 20000 | Minimum trim target in tokens. |

### Authentication

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_API_KEYS | string | "" | Comma-separated label=key pairs for multi-tenant auth. |
| CTXGATE_API_KEY | string | "" | Legacy single API key (tenant "local"). |
| CTXGATE_ADMIN_KEY | string | "" | Admin key for /metrics, /api/* endpoints. |
| CTXGATE_ALLOW_NO_AUTH | string | 0 | Set 1 to disable auth (loopback only). **Never use behind a reverse proxy.** |
| CTXGATE_HOST | string | 127.0.0.1 | Bind address. |
| CTXGATE_PROXY_PORT | int | 9201 | Listen port. |

### Session Identity

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_SESSION_IDENTITY | string | goose | goose = X-Session-ID header or Goose SQLite lookup. hash = deterministic SHA-256. |
| CTXGATE_GOOSE_INTEGRATION | string | 0 | Set 1 to enable Goose session resolution. |
| CTXGATE_SESSION_TTL_HOURS | int | 12 | Session state TTL in hours. |

### External LM (Mistral AI)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_LM_ENABLED | string | 1 | Set 0 to disable all LM calls. Worker idles; windowing continues. |
| CTXGATE_LM_URL | string | https://api.mistral.ai/v1 | External LM base URL. |
| CTXGATE_LM_MODEL | string | mistral-small-latest | External LM model name. |
| CTXGATE_LM_API_KEY | string | "" | External LM API key. |
| CTXGATE_LM_WORKERS | int | 10 | Number of concurrent LM consumer workers. |

### Memory Worker

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_MEMORY_WORKER | string | 1 | Set 0 to disable the memory worker loop. |
| CTXGATE_WORKER_POLL | float | 2.0 | Job queue poll interval in seconds. |
| CTXGATE_WORKER_MAX_ATTEMPTS | int | 3 | Max retry attempts per job. |
| CTXGATE_WORKER_OUTAGE_TTL | int | 1800 | Outage detection TTL in seconds. |
| CTXGATE_MEMORY_TTL_DAYS | int | 90 | Memory entry TTL in days. |
| CTXGATE_KNOWLEDGE_EXTRACT | string | 1 | Enable cross-session knowledge extraction. |

### vLLM Connection

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_VLLM_READ_TIMEOUT | int | 300 | Read timeout in seconds. |
| CTXGATE_VLLM_CONNECT_TIMEOUT | int | 10 | Connect timeout in seconds. |
| CTXGATE_VLLM_WRITE_TIMEOUT | int | 120 | Write timeout in seconds. |
| CTXGATE_VLLM_POOL_TIMEOUT | int | 30 | Connection pool timeout in seconds. |

### Advanced

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| CTXGATE_STABLE_ELIDE | string | 1 | Enable stable tool-body elision for prefix-cache friendliness. |
| CTXGATE_INJECT_EPOCH_FREEZE | string | 1 | Enable epoch-freeze injection caching. |
| CTXGATE_INJECT_MAX_TOKENS | int | 3000 | Max tokens for injection block (normal). |
| CTXGATE_INJECT_MAX_TOKENS_RECAP | int | 5000 | Max tokens for injection block (recap). |
| CTXGATE_MAX_CONTINUATIONS | int | 5 | Max continuation attempts for truncated responses. |
| CTXGATE_MAX_BODY_BYTES | int | 20971520 | Max request body size (20 MB). |
| CTXGATE_MIN_TEMPERATURE | float | 0.3 | Minimum temperature forwarded to vLLM. |
| CTXGATE_TENANT_RATE_PER_SEC | float | 0.0 | Per-tenant rate limit (0 = disabled). |
| CTXGATE_TENANT_MAX_CONCURRENCY | int | 0 | Per-tenant max concurrent requests (0 = disabled). |
| CTXGATE_TEST_ENDPOINTS | string | 0 | Set 1 to enable /_test/ debug endpoints. |
| CTXGATE_SKIP_DOTENV | string | — | Set 1 to skip .env file loading. |
| CTXGATE_ENV_PATH | string | .env | Path to the environment file. |

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/PawelWos1987/local-llm-ctxgate-proxy.git
cd local-llm-ctxgate-proxy

# 2. Create venv
python3.12 -m venv /opt/ctxgate-proxy/.venv
/opt/ctxgate-proxy/.venv/bin/pip install -e ".[dev]"

# 3. Start PostgreSQL
docker compose up -d

# 4. Configure
cp .env.example .env
# Edit .env: set CTXGATE_DB_DSN, CTXGATE_QWEN_TOKENIZER, CTXGATE_VLLM_URL, CTXGATE_VLLM_MODEL

# 5. Validate
/opt/ctxgate-proxy/.venv/bin/python proxy/app.py --check-config

# 6. Run (development, no auth, loopback only)
CTXGATE_ALLOW_NO_AUTH=1 CTXGATE_HOST=127.0.0.1 /opt/ctxgate-proxy/.venv/bin/python -u proxy/app.py
```

Point your AI coding agent at `http://127.0.0.1:9201/v1` as the OpenAI-compatible API endpoint.

---

## Usage

### From an OpenAI-Compatible Client

Any client that speaks the OpenAI Chat Completions API can use ctxgate as its backend:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9201/v1",
    api_key="your-api-key",  # or "no-auth" if CTXGATE_ALLOW_NO_AUTH=1
)

response = client.chat.completions.create(
    model="Qwen3.8-27B",
    messages=[
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Help me refactor this module."},
    ],
    stream=True,
)

for chunk in response:
    print(chunk.choices[0].delta.content, end="", flush=True)
```

### From Goose

Goose can be configured to use ctxgate as its LLM backend by pointing its OpenAI-compatible provider at the proxy. The proxy reads the `X-Session-ID` header that Goose sends to maintain session continuity.

### Health and Metrics Endpoints

```bash
# Liveness (no auth)
curl http://127.0.0.1:9201/health

# Readiness (no auth)
curl http://127.0.0.1:9201/ready

# Internal metrics (admin key required)
curl -H "Authorization: Bearer $CTXGATE_ADMIN_KEY" http://127.0.0.1:9201/metrics

# Prometheus format (admin key required)
curl -H "Authorization: Bearer $CTXGATE_ADMIN_KEY" http://127.0.0.1:9201/metrics/prometheus

# Active sessions (admin key required)
curl -H "Authorization: Bearer $CTXGATE_ADMIN_KEY" http://127.0.0.1:9201/api/sessions
```

---

## Testing

### Unit Tests

```bash
make test-unit
# or
pytest tests/ -x -q
```

The test suite contains 52 primary test files in `tests/` plus 21 secondary test files in `_local/tests/`. Most are pure unit tests that run without external services. A subset are integration/live tests that require a running proxy, PostgreSQL, and vLLM server — these are excluded from the default pytest run via the `SKIP_LIVE` list in `tests/conftest.py`.

### Running Integration Tests

Integration tests must be run standalone against a live environment:

```bash
python tests/test_unit_integ.py
python tests/test_e2e_4b_memory.py
python tests/test_scenario_replay.py
```

### Test Infrastructure

- `tests/fakes.py` — Deterministic SSE fakes for streaming tests
- `tests/mock_vllm.py` — Full mock vLLM server for testing without a GPU
- `tools/mock_upstream.py` — Mock upstream for benchmarking (SSE streaming)
- `tests/goldens/` — 8 golden SSE files for regression testing
- `tests/conftest.py` — Shared fixtures, SKIP_LIVE list, pytest configuration

> **Note:** No aggregate test pass/fail status is recorded in the repository. The CI workflow is manual-trigger only and runs a single test file.

---

## Logging and Observability

### Log Files

| Service | Log File | Configured By |
|---------|----------|---------------|
| Proxy | proxy.log | systemd StandardOutput/StandardError |
| Worker | worker.log | systemd StandardOutput/StandardError |
| Dashboard | dashboard.log | systemd (if deployed) |

### Metrics Endpoints

| Endpoint | Auth | Format | Description |
|----------|------|--------|-------------|
| GET /health | None | JSON | Liveness: status, version, session count, FD stats |
| GET /ready | None | JSON | Readiness: DB connectivity, tokenizer loaded |
| GET /metrics | Admin | JSON | Internal metrics: request counts, latency, cache stats |
| GET /metrics/prometheus | Admin | Prometheus | Prometheus scrape format |
| GET /api/metrics | Admin | JSON | Detailed metrics with per-session breakdown |
| GET /api/sessions | Admin | JSON | Active session list with token counts |
| GET /api/recent-calls | Admin | JSON | Recent call log |
| GET /api/errors | Admin | JSON | Recent error log |
| GET /api/memory-summary | Admin | JSON | Memory statistics summary |

### Dashboard

The dashboard (dashboard/dashboard.py, 2,432 lines) provides a web interface for:
- Real-time health monitoring
- Request and session metrics
- Configuration viewing and editing
- Log tailing
- Service control (start/stop/restart via systemd)

---

## Performance

### Proxy Overhead (Measured 2026-10-10)

Measured against a mock upstream with ~20,000-token inputs. The proxy adds minimal latency overhead:

| Config | TTFT Overhead p50 | TTFT Overhead p95 | Total Stream | Failures |
|--------|-------------------|-------------------|--------------|----------|
| 100 req, concurrency=1 | +31 ms | +33 ms | Proxy 141 ms faster | 0/100 |
| 100 req, concurrency=4 | +91 ms | +131 ms | Proxy 89 ms faster | 0/100 |
| 50 req, concurrency=1 | +33 ms | +41 ms | Proxy 797 ms faster | 0/50 |

**Source:** `tasks/rwfix/bench_20k_c1.json`, `tasks/rwfix/bench_20k_c4.json`, `tasks/rwfix/bench_quick.json`

The proxy is consistently faster in total stream time than direct access to the mock. The TTFT overhead (31–91 ms) is the cost of token counting, context management, and memory injection.

### KV-Cache Hit Rate (Measured 2026-10-02)

| Metric | Value | Target | Met? |
|--------|-------|--------|------|
| Proxy real-traffic weighted rate | 60.5% | 90% | No |
| vLLM global weighted rate | 83.5% | — | — |
| 20-turn benchmark session | 53.7% | — | — |
| 8-turn benchmark session | 41.3% | — | — |

**Root cause:** The 90% target was not met due to a limitation in vLLM 0.30's hybrid Mamba/attention model architecture. The reusable prefix is capped at ~1,664 tokens (104 × 16-token blocks). Direct vLLM access (bypassing the proxy) shows the identical cache pattern. This is **not a proxy bug**.

**Source:** `_local/benchmarks/p3_results.json`

### Inference Server Baseline (2026-10-01)

| Server | Gen Throughput | Source |
|--------|---------------|--------|
| vLLM 0.30 (Qwen3.8-27B W4A16) | 174 tok/s | _local/notes/PHASE0_BENCH_VLLM.txt |
| LM Studio (same model) | 23 tok/s | _local/notes/PHASE0_BENCH_LMSTUDIO.txt |

vLLM is ~7.5× faster than LM Studio for this model.

### Operational Scale (Accumulated)

From `injection_metrics.json` (live system, accumulated over multiple weeks):

| Metric | Value |
|--------|-------|
| Total requests processed | 24,243 |
| Task memory injections | 7,586 (1.57M tokens) |
| Knowledge injections | 20,052 (2.48M tokens) |
| Working memory injections | 6,783 (164K tokens) |

### What Has NOT Been Measured

- No formal TTFT/latency benchmarks against a real vLLM server (only against a mock)
- No concurrent-load stress tests beyond 4 simultaneous requests
- No memory/GPU utilization measurements under sustained load
- No comparison across different model sizes or quantizations
- No multi-GPU scaling tests (the tested environment uses 2x RTX 5070 Ti with tensor parallelism)

---

## Limitations and Known Issues

### Architectural Limitations

- **Single-user design.** The proxy is designed for one developer on one machine. Multi-tenant features exist (API keys, rate limiting) but have not been tested under adversarial multi-tenant load.
- **Single vLLM backend.** The proxy is configured for one upstream vLLM server. There is no load balancing, failover, or multi-model routing.
- **Qwen tokenizer required.** Token counting depends on a specific Qwen tokenizer file. Using a different model family may produce inaccurate token counts.
- **External LM dependency.** Context summarization and memory extraction depend on an external LM (Mistral AI by default). If the external LM is unavailable, the worker idles but the proxy continues serving with degraded memory capabilities.

### Known Issues

| Issue | Status | Detail |
|-------|--------|--------|
| Prefix cache hit rate below target | Known limitation | 60.5% measured vs 90% target. Root cause is vLLM 0.30's hybrid Mamba/attention model architecture, not a proxy bug. |
| config.yaml not read by proxy | Legacy artifact | The file contains stale values. All configuration is environment-variable driven. |
| deploy/ctxgate-dashboard.service missing | Incomplete | Referenced by make install-systemd but not present in the repository. |
| Docker Compose applies only migrations 001–007 | Incomplete | Migrations 008–013 must be applied manually. |
| schema/012_session_windows.sql gitignored | Inconsistency | This migration file is in .gitignore and will not be present in fresh clones. |
| CI/CD is minimal | Known gap | Manual-trigger workflow runs a single test file. No automated unit, integration, or lint pipeline. |
| No license selected | Open | The repository has no LICENSE file. |

### Not Tested

- Multi-GPU deployments beyond the tested 2x RTX 5070 Ti configuration
- Different model families (LLaMA, Mistral, Gemma)
- High-concurrency scenarios (10+ simultaneous sessions)
- Long-term stability beyond several weeks of continuous operation
- Network partition or vLLM crash recovery under load
- Security under adversarial conditions

---

## Security and Privacy

- **Local-first by design.** The proxy binds to 127.0.0.1 by default. All data (conversations, memory, sessions) stays on the local machine and local PostgreSQL.
- **No data leaves the machine** except for summarization/extraction requests to the external LM (Mistral AI). Disable with CTXGATE_LM_ENABLED=0 if you want zero external calls.
- **API key authentication** is supported for non-loopback deployments. No-auth mode is restricted to loopback addresses.
- **systemd hardening:** NoNewPrivileges, PrivateTmp, ProtectSystem=full, ProtectHome=read-only are all enabled in the service units.
- **No TLS termination** is built into the proxy. For non-local deployments, terminate TLS at a reverse proxy (nginx) and use API key authentication.
- **Database credentials** must be set in .env (gitignored). The repository ships with CHANGE_ME placeholders that cause startup failure by design.

> **This project is not a hardened multi-tenant service.** It has no rate limiting by default, no request signing, no audit logging, and no input sanitization beyond what the LLM pipeline provides. Use it accordingly.

---

## Repository Structure

```
ctxproxy/
├── proxy/
│   └── app.py              # FastAPI proxy (6,359 LOC)
├── worker/
│   └── worker.py           # Memory extraction worker (1,272 LOC)
├── dashboard/
│   └── dashboard.py        # Web dashboard (2,432 LOC)
├── schema/
│   ├── 001_init.sql
│   ├── 002_knowledge.sql
│   ├── 003_memory_worker.sql
│   ├── 004_memory_ttl.sql
│   ├── 005_seed_knowledge.sql
│   ├── 006_deliverables.sql
│   ├── 007_memory_scoring.sql
│   ├── 008_memory_keynorm.sql
│   ├── 009_idx_memories_task_cat_key.sql
│   ├── 010_deliverables_unique.sql
│   ├── 011_phase_summaries.sql
│   ├── 012_session_windows.sql   # NOTE: gitignored
│   └── 013_memory_hardening.sql
├── deploy/
│   ├── ctxgate-proxy.service
│   ├── ctxgate-worker.service
│   └── README.md
├── tests/                    # 52 test files + infrastructure
├── tools/                    # Utility scripts, benchmarks
├── probes/                   # Diagnostic probe scripts
├── .github/
│   └── workflows/            # CI/CD (manual trigger + CodeQL)
├── .env.example              # Environment variable template
├── docker-compose.yml        # PostgreSQL 16
├── Dockerfile                # Single-stage container
├── Makefile                  # 13 targets
├── pyproject.toml            # Python project metadata
├── README.md
├── ARCHITECTURE.md
├── BUG_CATALOG.md
├── CHANGELOG.md
├── SECURITY.md
└── CONTRIBUTING.md
```

---

## Related Documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — Component-level technical design
- [BUG_CATALOG.md](BUG_CATALOG.md) — Known bugs and their status
- [CHANGELOG.md](CHANGELOG.md) — Version history
- [SECURITY.md](SECURITY.md) — Security considerations
- [CONTRIBUTING.md](CONTRIBUTING.md) — Contribution guidelines
- [deploy/README.md](deploy/README.md) — Deployment instructions

---

## AI-Driven Development & Attribution

**Project ownership and requirements input:** PawelWos1987 — [wos.pawel@gmail.com](mailto:wos.pawel@gmail.com).

**Development approach:** According to the project owner's declaration, the project's implementation was created through prompt-driven AI workflows, with zero manually written human code. Prompts were prepared using ChatGPT, Claude, and chat.deepseek based on requirements, instructions, and input supplied by PawelWos1987.

The project owner supplied the requirements and direction for the work. AI-generated implementation does not imply that the AI systems independently defined the product requirements or that every implementation outcome was automatically correct. Actual test results and performance claims in this repository are documented separately and must be supported by the evidence described in this documentation.

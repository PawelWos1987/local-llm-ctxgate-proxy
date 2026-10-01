# local-llm-ctxgate-proxy Architecture

A lightweight local context gate proxy for long-running AI coding agents.

local-llm-ctxgate-proxy (Python/FastAPI, ~1390 LOC) sits between Goose (the AI agent) and vLLM (the LLM inference server). It enforces context-window boundaries, supplements context with conditional memory injection, and streams responses — all with < 5 ms added latency on the hot path.

---

## 1. Three-Layer Separation

The system is built on three distinct layers, each responsible for a different timescale of memory:

| Layer | Role | Timescale |
|---|---|---|
| **Goose compaction** | Short-term continuity — in-context summarization | Per-session, triggered at 65% of window |
| **ctxgate-proxy** | Context boundary + small conditional memory supplement | Per-request, deterministic |
| **4B memory worker** | Async durable-memory extraction | Per-turn, background |

### 1.1 Goose Compaction (Short-Term Continuity)

Goose's in-context summarization is **enabled** at GOOSE_AUTO_COMPACT_THRESHOLD = 0.65. On an 84,000-token window this triggers at 54,600 total tokens — a deliberate safety margin below ctxgate-proxy's 64k input trim cap.

Why 0.65:
- **Above 0.75**: input approaches the 64k trim line; ctxgate-proxy would start dropping middle messages (lossy) before Goose's cleaner summarization runs.
- **Below 0.5**: Goose compacts too aggressively, summarizing context that still fits. Wasted summarization calls, added latency.
- **0.65**: leaves a comfortable margin. Non-lossy in practice.

Goose compaction is the **index** — it tells the agent which files and sessions to re-read. It keeps a single session oriented.

### 1.2 ctxgate-proxy (Context Boundary + Conditional Memory)

The proxy is a **deterministic gate** on every request:

1. **Token-aware assembly**: counts tokens using the Qwen tokenizer (not byte-estimates). Assembles the prompt from system + tools + messages, trimming oldest middle messages first when input exceeds MAX_INPUT (64,000 tokens).
2. **Conditional memory injection**: before sending to vLLM, checks if relevant memories exist in PostgreSQL that are NOT already in the current context. Uses a 60% overlap dedup gate — if the fact is already visible to the model, it is NOT re-injected. This keeps the hot path at < 5 ms when context is sufficient.
3. **SSE streaming passthrough**: responses stream directly from vLLM to Goose with no buffering.
4. **Session isolation**: each Goose session gets its own task_id. Memories are scoped per-task. Cross-session knowledge sharing is opt-in via the knowledge table.

The proxy **never blocks** on the 4B model. Memory extraction is fully async.

### 1.3 4B Memory Worker (Async Durable Memory)

A separate Python process (systemd user service) polls proxy.memory_jobs and calls the 4B LM Studio model to extract structured memories from conversation events.

**Model**: qwen3-4b-instruct-2507 (higher quantization, LM Studio :1234)

**Structured Output Schema** (enforced via response_format.type = json_schema):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["memory_actions", "state_update"],
  "properties": {
    "memory_actions": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["action", "type", "importance", "title", "content", "source_event_id"],
        "properties": {
          "action": { "type": "string", "enum": ["NEW", "UPDATE", "SUPERSEDE", "DUPLICATE", "NO_CHANGE"] },
          "type": { "type": "string", "enum": ["DECISION", "FINDING", "FAILURE", "TODO", "CONSTRAINT", "FILE", "STATE", "FACT"] },
          "importance": { "type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"] },
          "title": { "type": "string" },
          "content": { "type": "string" },
          "source_event_id": { "type": "string" }
        }
      }
    },
    "state_update": {
      "type": "object",
      "additionalProperties": false,
      "required": ["changed", "current_state", "current_subtask"],
      "properties": {
        "changed": { "type": "boolean" },
        "current_state": { "type": ["string", "null"] },
        "current_subtask": { "type": ["string", "null"] }
      }
    }
  }
}
```

**Field mapping** (schema → DB columns):

| Schema field | DB column | Notes |
|---|---|---|
| title | key | Normalized for dedup (lowercase, alnum-only) |
| content | value | Free-text |
| type | category | Free-text (DECISION, FINDING, etc.) |
| importance | importance | String→int map: CRITICAL=10, HIGH=7, NORMAL=5, LOW=2 |
| source_event_id | source_event_id | UUID, traceability |
| — | model_name | Auto-populated: qwen3-4b-instruct-2507 |

**Dedup & lifecycle**:
- **NEW**: INSERT if no matching key (normalized) exists for the task
- **UPDATE**: UPDATE value/importance on existing key
- **SUPERSEDE**: mark old row active=false, set superseded_by to new row's ID, INSERT new
- **DUPLICATE / NO_CHANGE**: no-op, job marked done with applied=0

**Outage handling**: if LM Studio is unreachable, jobs stay pending with exponential backoff (2s → 60s cap) for up to CTXGATE_WORKER_OUTAGE_TTL (default 1800s = 30 min). Only after that window expires are jobs marked failed. The attempts column tracks retry count.

**Pre-load**: on startup, the worker checks GET /v1/models. If the model is absent, it triggers POST /v1/models/load and polls for up to 90s. This prevents the 120s request timeout from being consumed by model load time.

**No loops**: the 4B's output NEVER enqueues a new memory job. Only original Goose/user/tool events (enqueued by proxy/app.py) create jobs.

---

## 2. Data Flow

```
Goose Agent
    |
    |  POST /v1/chat/completions  (http://127.0.0.1:9200)
    v
+-------------------------------------------+
|  ctxgate-proxy (FastAPI, :9200)           |
|                                           |
|  1. Token-count input (Qwen tokenizer)   |
|  2. Trim to MAX_INPUT=64000 if needed    |
|  3. Conditional memory injection         |
|     (dedup 60% overlap + relevance gate) |
|  4. Forward to vLLM                      |
|  5. Stream SSE response back to Goose    |
|  6. Enqueue memory job (async, non-block)|
+------------------+------------------------+
                   |
                   |  http://127.0.0.1:29000/v1
                   v
              vLLM (Qwen3.8-27B, 130k window)

                   |  (async, separate process)
                   v
+-------------------------------------------+
|  4B Memory Worker (systemd user service) |
|                                           |
|  1. Poll memory_jobs (FOR UPDATE SKIP    |
|     LOCKED, parallelism=1)               |
|  2. Call qwen3-4b-instruct-2507          |
|     (LM Studio :1234, json_schema)      |
|  3. Validate + dedup + apply to PG      |
|  4. Update working_memory               |
|  5. Mark job done/failed                |
+-------------------------------------------+
                   |
                   v
            PostgreSQL (ctxproxy DB)
            - proxy.tasks
            - proxy.events
            - proxy.memories  (model_name column)
            - proxy.working_memory
            - proxy.memory_jobs  (attempts column)
            - proxy.knowledge
```

---

## 3. Key Design Decisions

| Decision | Rationale |
|---|---|
| **Conditional injection** (not blanket) | Zero hot-path overhead when context is already sufficient. 60% overlap gate + relevance check. |
| **Async 4B worker** (not inline) | 27B never waits for 4B. A bad 4B response never blocks the agent. |
| **json_schema response_format** | LM Studio 4B only accepts json_schema (not json_object). Strict schema prevents malformed output. |
| **additionalProperties: false** | 4B models hallucinate extra fields. This kills that failure mode at the API level. |
| **Token-aware trimming** (not char-based) | Qwen tokenizer gives accurate counts. Char-based estimates are 20-30% off. |
| **Per-task memory scoping** | Sessions don't pollute each other. Cross-session sharing is opt-in via knowledge table. |
| **Outage TTL + backoff** | Prevents burning 3 attempts in 6s during a 30-min LM Studio outage. Jobs stay pending, retry with exponential backoff. |
| **model_name column** | Tracks which 4B model produced each memory. Essential when switching quantizations. |

---

## 4. Security Boundary

- **Localhost only**: both the proxy (:9200) and LM Studio (:1234) bind to 127.0.0.1. No external exposure.
- **Single-user**: designed for one developer on one machine. No authentication, no multi-tenancy.
- **PostgreSQL**: local instance, password-protected. The DSN is in a systemd user service file (0600) and .env (gitignored).
- **No PII in memories**: the 4B extracts technical facts (config values, decisions, file paths). The prompt explicitly instructs it to ignore personal data.
- **See SECURITY.md** for the full threat model and vulnerability reporting process.

---

## 5. File Layout

```
local-llm-ctxgate-proxy/
├── proxy/
│   └── app.py              # FastAPI proxy (~1390 LOC)
├── worker/
│   └── worker.py           # 4B memory worker (~480 LOC)
├── schema/
│   ├── 001_init.sql        # Core tables (tasks, events, memories, working_memory)
│   ├── 002_knowledge.sql   # Cross-session knowledge sharing
│   └── 003_memory_worker.sql  # memory_jobs + attempts + model_name
├── tests/
│   ├── test_architecture.py  # 11-check architecture suite (21 test files total)
│   ├── test_e2e_4b_memory.py # End-to-end 4B pipeline
│   ├── test_perf_abc.py      # Performance benchmarks
│   └── test_8m_longrun.py    # 8M-token long-run stress test
├── .github/
│   ├── workflows/
│   │   ├── ci.yml          # Python 3.11/3.12 matrix + PG16
│   │   └── codeql.yml      # SAST (push/PR + weekly)
│   └── dependabot.yml      # pip + github-actions weekly
├── .env.example
├── ARCHITECTURE.md
├── README.md
├── SECURITY.md
├── LICENSE
└── requirements.txt
```

---

## 6. Environment Variables

| Variable | Default | Description |
|---|---|---|
| CTXGATE_DB_DSN | postgresql://postgres:postgres@127.0.0.1:5432/ctxproxy | PostgreSQL connection string |
| CTXGATE_VLLM_URL | http://127.0.0.1:29000/v1 | vLLM server URL |
| CTXGATE_VLLM_MODEL | Qwen3.8-27B | Model name served by vLLM |
| CTXGATE_QWEN_TOKENIZER | /path/to/tokenizer.json | Qwen tokenizer for accurate counting |
| CTXGATE_LM_URL | http://127.0.0.1:1234/v1/chat/completions | LM Studio 4B endpoint |
| CTXGATE_LM_MODEL | qwen3-4b-instruct-2507 | 4B model name |
| CTXGATE_WORKER_POLL | 2.0 | Worker poll interval (seconds) |
| CTXGATE_WORKER_MAX_ATTEMPTS | 3 | Max retries per job (non-outage) |
| CTXGATE_WORKER_OUTAGE_TTL | 1800 | Outage backoff window (seconds) |
| CTXGATE_WORKER_MAX_TOKENS | 512 | Max output tokens for 4B |
| CTXGATE_MAX_CONTEXT | 84000 | Total context window (tokens) |
| CTXGATE_MAX_INPUT | 64000 | Max input tokens before trimming |
| CTXGATE_MAX_OUTPUT | 18000 | Max output tokens |
| CTXGATE_SAFETY_MARGIN | 2000 | Token safety margin |
| CTXGATE_PROXY_PORT | 9200 | Proxy listen port |
| CTXGATE_API_KEY | (off) | Optional bearer auth on /v1/chat/completions |
| CTXGATE_MAX_BODY_BYTES | 20971520 | Max request body size (20 MB) |
| CTXGATE_WORKER_CONCURRENCY | 1 | Worker parallelism |

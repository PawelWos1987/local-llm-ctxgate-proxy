# FINAL PLAN: ctxgate-proxy 4B Memory Worker + Trim Summarization Pipeline

## Status: IMPLEMENTED

## What Was Done

### 1. New DB Table: proxy.session_summaries

```sql
CREATE TABLE proxy.session_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES proxy.tasks(id) ON DELETE CASCADE,
    session_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    trimmed_msg_count INT DEFAULT 0,
    trimmed_tokens INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

### 2. New Functions in proxy/app.py (lines 21-208)

| Function | Purpose |
|----------|---------|
| `_call_4b(messages, max_tokens, json_mode)` | Async HTTP call to LM Studio 4B model. Supports JSON mode (structured output) and plain text mode. |
| `_store_memory_actions(task_uuid, actions, source_event_id)` | Persists NEW/UPDATE/SUPERSEDE memory actions from 4B response into proxy.memories. |
| `_update_working_memory(task_uuid, state_update)` | Upserts task state into proxy.working_memory from 4B state_update. |
| `_process_memory_job(job_id, task_uuid, event_id)` | Orchestrates a single memory job: fetch event -> call 4B -> store results. |
| `_memory_worker_loop()` | Background loop: polls proxy.memory_jobs every 5s, processes pending jobs. |
| `_summarize_trimmed_messages(task_uuid, session_key, trimmed_messages)` | When context is trimmed, sends cut messages to 4B for running summary. Stores in session_summaries. |
| `_fetch_session_summary(task_uuid, budget)` | Fetches latest session summary for injection into future contexts. |

### 3. Integration Points

| Location | Change |
|----------|--------|
| `lifespan()` (line 421) | Starts `_memory_worker_loop()` as background task; cancels on shutdown. |
| `build_context()` (line 606) | New signature: `build_context(request_messages, task_uuid, session_key)`. Captures dropped messages on trim, fires `asyncio.ensure_future(_summarize_trimmed_messages(...))`. |
| `fetch_task_memory()` (line 847) | Injects session summary (budget 600 tokens) after working memory. |
| `chat_completions()` (line 1155) | Resolves `task_uuid` before calling `build_context`, passes it through. |

### 4. How It Works (End-to-End Flow)

```
Request arrives
    |
    v
chat_completions()
    |-- resolve task_uuid
    |-- build_context(messages, task_uuid, session_key)
    |       |-- sanitize, strip reasoning
    |       |-- if tokens > MAX_INPUT (64k):
    |       |       |-- trim_context() -> keeps system + first_user + recent tail
    |       |       |-- dropped = messages that were cut
    |       |       |-- asyncio.ensure_future(_summarize_trimmed_messages(...))
    |       |               |-- format dropped messages
    |       |               |-- fetch prior summary from session_summaries
    |       |               |-- call 4B: "Update summary with these cut messages"
    |       |               |-- store updated summary in session_summaries
    |       |
    |       v
    |-- fetch_task_memory()
    |       |-- inject working_memory (current state)
    |       |-- inject session_summary (running narrative)  <-- NEW
    |       |-- inject critical memories
    |       |-- inject relevant knowledge
    |
    v
Forward to vLLM (27B) with bounded context (<= 64k input + 18k output = 82k < 84k)
```

### 5. Memory Worker Loop (Async, Non-Blocking)

```
Every 5 seconds:
    |-- poll proxy.memory_jobs WHERE status='pending'
    |-- for each job:
    |       |-- fetch event content from proxy.events
    |       |-- fetch current working_memory
    |       |-- fetch recent memories (for dedup context)
    |       |-- call 4B with structured output schema
    |       |-- store memory_actions (NEW/UPDATE/SUPERSEDE) into proxy.memories
    |       |-- update working_memory from state_update
    |       |-- mark job as 'done'
```

### 6. LM Studio Configuration (Required)

| Setting | Value |
|---------|-------|
| Model | qwen3-4b-instruct-2507 |
| Context size | 8192 |
| Thinking/Reasoning | 0 |
| System prompt | (see _MEMORY_SYSTEM_PROMPT in app.py) |
| Structured Output | (see _MEMORY_SCHEMA - JSON with memory_actions + state_update) |
| Port | 1234 (default) |

### 7. Token Budget (Unchanged)

| Component | Tokens |
|-----------|--------|
| MAX_CONTEXT | 84,000 |
| MAX_INPUT | 64,000 |
| MAX_OUTPUT | 18,000 |
| SAFETY_MARGIN | 2,000 |
| Session summary injection | <= 600 (within memory budget) |
| Working memory injection | <= 800 |
| Knowledge injection | <= 400 |

### 8. What This Solves

| Problem | Solution |
|---------|----------|
| Agent loses original goal after window cuts turn 1 | Session summary preserves GOAL across all trims |
| Agent forgets decisions made 20 turns ago | 4B extracts DECISION memories, injected via fetch_task_memory |
| Agent re-litigates settled questions | SUPERSEDE actions mark old memories as inactive |
| Agent loses track of progress | Working memory updated with current_state + current_subtask |
| Agent violates constraints stated early | CONSTRAINT memories persist and are re-injected |
| Trimmed content is simply lost | Now captured, summarized, and stored in DB |

### 9. File Metrics

| Metric | Before | After |
|--------|--------|-------|
| proxy/app.py lines | 2,127 | 2,347 (+220) |
| Functions | 61 | 68 (+7) |
| DB tables | 6 | 7 (+session_summaries) |
| Background tasks | 0 | 1 (memory worker loop) |

### 10. Verification

- [x] Syntax: `ast.parse()` passes
- [x] All 7 new functions present
- [x] Integration points wired (lifespan, build_context, fetch_task_memory, chat_completions)
- [x] DB table created with indexes
- [x] 4B model reachable at 127.0.0.1:1234 (0.9s response time)
- [x] No impact on existing 87/87 test suite (pure function tests unchanged)

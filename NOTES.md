# CTXGATE HARDENING PASS — IMPLEMENTATION PLAN (session 20261006_25)

Goal: make long autonomous Goose sessions durable + failure-safe. Do NOT rewrite architecture.
Preserve: sticky-window, phase-summary, root-summary, PostgreSQL-memory, SSE-heartbeat, continuation, circuit-breaker, prefix-cache design.

## FILES
- proxy/app.py (5240 lines) — main proxy (ALL context + streaming changes here)
- worker/worker.py (864 lines) — 4B memory worker
- schema/003_memory_worker.sql (+ 001_init.sql for events/memories/jobs)
- tests/ — add test_output_integrity.py (A-I), extend existing

## KEY LINE MAP (proxy/app.py)
- Config consts: 892-915 (MAX_CONTEXT, MAX_INPUT, MAX_OUTPUT=22500, SAFETY_MARGIN=3500, MIN_OUTPUT=16000, PINNED_USER_MAX_CHARS=16000, PINNED_USER_FAR_CHARS=2000, PINNED_USER_FAR_THRESHOLD=8, MAX_CONTINUATIONS=5, WORKER_BACKPRESSURE=50)
- _classify_truncation: 1387
- sanitize_tool_calls: 1405
- _prep_messages: 1442
- _trim_target: 1479
- _norm_content: 1489
- _msg_anchor: 1501 (hashes role|content[:300]|tool_call_id)
- _seed_sig: 1506
- _new_window_state: 1520
- _window_valid: 1535
- STUB_TEXT: 1553
- _make_pinned_copy: 1554  <-- BUG: no ctxgate_pinned marker; keeps only c[:CAP] (head only)
- _pinned_user_copy: 1565
- _kept_messages: 1574
- _newest_user_idx: 1587
- _recut_to: 1595
- _recut: 1649
- _output_budget: 1661 (returns min(MAX_OUTPUT, MAX_CONTEXT-input-SAFETY_MARGIN))
- _protected_indices: 1667  <-- protects seed/stub/pinned/newest-6, but pinned never flagged
- _elided_tool_content: 1692 (1000 head + marker + 1000 tail)
- _stable_elide_inplace: 1701 (elides tool>2500 chars in [start,end))
- _stable_elide_idx: 1721 (index of 4th-newest tool result; [cut,idx) elidable)
- _emergency_shrink: 1736  <-- BUG: step (a) elides ALL tool>2500 incl newest 4 (ignores protected); step (c) hard-truncates largest (can hit protected)
- build_context: 1867 (fast path 1876-1924, slow path 1925-1987)
- _truncate_message_content: 1988
- trim_context: 2012
- fetch_task_memory: 2410
- chat_completions: 2859 (budget+413 at 3030-3042; builds vllm_body; calls stream/forward)
- forward_to_vllm: 3318 (non-stream; continuation loop 3383; 400 retry 3350)
- _safe_truncate: 3457
- _sse_content: 3477
- stream_to_vllm: 3485 (THE critical path; generate() 3488; main loop 3540; continuation 3713; retry loop 3758; final chunk 3900; [DONE] 3902)
- _enqueue_memory_job: 4013  <-- BUG: backpressure DROPS job (return) when pending>WORKER_BACKPRESSURE; 5000-char cap
- _resolve_task: 4056

## worker/worker.py
- Config: 40-70 (DSN, POLL=2, MAX_ATTEMPTS=3, OUTAGE_TTL=1800, CONSUMERS=10, EVENT_EXCERPT_CHARS=2500, WM_EXCERPT_CHARS=1500, MEMORY_TTL_DAYS=90)
- Single-instance lock: flock + heartbeat (LOCK_FILE, LOCK_TTL=30)
- validate_response: ~150
- build_payload: ~190 (role=user only currently)
- Polls memory_jobs FOR UPDATE SKIP LOCKED; applies NEW/UPDATE/SUPERSEDE/DEDUP to proxy.memories; updates working_memory
- Outage: jobs stay pending w/ backoff until OUTAGE_TTL then failed

## SCHEMA (proxy schema)
- proxy.events (task_id, seq, role, content) — content capped 5000 at insert
- proxy.memory_jobs (task_id, event_id, status, attempts) — pending/processing/done/failed
- proxy.memories (task_id, active, importance, title, content, source_event_id, status, model_name)
- proxy.working_memory, proxy.phase_summaries, proxy.session_summaries, proxy.session_windows

## CONFIRMED BUGS TO FIX
1. _make_pinned_copy: NO ctxgate_pinned marker -> _protected_indices never protects it. FIX: add ctxgate_pinned=True + stable source anchor; head+tail (not head-only) truncation; keep first 300 chars verbatim.
2. _emergency_shrink step (a): elides ALL tool>2500 including newest 4. FIX: use canonical protected_tool_groups; skip newest-4 tool bodies.
3. _emergency_shrink step (c): hard-truncates largest msg (may be protected). FIX: if protected can't fit -> structured 413 context-capacity failure, never silent truncate.
4. No total-output guard. ADD CTXGATE_MAX_TOTAL_OUTPUT=50000, CTXGATE_MIN_CONTINUATION_OUTPUT=1024. remaining=MAX_TOTAL-total_out; max_tokens=min(budget,MAX_OUTPUT,remaining).
5. Tool calls NOT atomic: only seg_tool_calls_seen bool. ADD ToolCallAccumulator (id,index,name,args,valid,complete). Buffer tool-call deltas until complete+valid; never stream malformed partial.
6. Continuation uses full_content only -> unsafe w/ tool calls. FIX state machine: text-truncated->continue; incomplete tool call->NO continue, finish length; complete tool set->terminate (let Goose run tools); tool set+truncated->terminate as truncated.
7. Backpressure DROPS memory jobs. FIX: report lag/metrics only, never drop eligible durable event.
8. No structured ctxgate SSE metadata. ADD to final chunk: {ctxgate:{truncated,reason,continuations_used,total_output_tokens,tool_calls_complete,tool_calls_emitted,tool_call_truncated?}}.
9. Truncated paths use finish_reason=stop. FIX: use "length" for incomplete; never fake "stop".
10. 5000-char event cap: use bounded/chunked canonical envelope (don't silently raise arbitrary limit).

## CANONICAL HELPERS TO CREATE (single source of truth, all paths use)
- protected_tool_groups(messages)->set: newest 4 tool-result idx + their assistant tool_calls idx.
- ctxgate_meta(...)->dict: structured metadata builder.
- ToolCallAccumulator: add_delta/is_complete/is_valid/to_tool_calls.
- verify_context_invariants(context, original)->list[str]: (1)newest user represented (2)pinned exists if cut (3)newest4 tool bodies byte-identical (4)no orphan tool (5)no assistant tool_calls w/o existing results (6)inside ceiling. Used by tests + prod diagnostics.
- _make_pinned_copy: head+tail + ctxgate_pinned + source anchor.

## EMERGENCY SHRINK CANONICAL ORDER
1 preserve seed; 2 preserve pinned newest-user; 3 preserve newest-4 tool groups; 4 elide older tool bodies; 5 drop oldest complete groups; 6 else structured 413.

## NEW ENV VARS
- CTXGATE_MAX_TOTAL_OUTPUT=50000
- CTXGATE_MIN_CONTINUATION_OUTPUT=1024
(keep all existing)

## METRICS TO ADD
output_total_budget_exhausted, output_truncated_total, output_truncated_by_reason{reason}, output_continuations_total, output_max_tokens_seen, tool_call_truncated, tool_call_suppressed, tool_call_complete, recent_tool_preservation_failures, pinned_user_preservation_failures, root_summary_lag, summary_retry_count, memory_jobs_created, memory_jobs_dropped(target 0), memory_worker_lag, memory_store_success, memory_store_failure, memory_retrieval_hits.

## TESTS (tests/test_output_integrity.py)
A total-output cap; B interrupted stream; C incomplete tool call; D complete tool call; E newest-4 tool bodies (8+ tool msgs, force all 4 paths); F pinned current instruction (head+tail); G root-summary continuity (EARLY/MID/LATEST milestone); H memory durability (e2e job->worker->memory->retrieve; restart-safe; no dup; backlog no-drop); I pathological protected overflow -> structured 413.

## STATUS
Analysis COMPLETE. Implementation IN PROGRESS.

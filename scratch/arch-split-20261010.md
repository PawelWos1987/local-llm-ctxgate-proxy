# Architectural Review: Path Split in ctxproxy/proxy/app.py

**Date:** 2026-10-10
**Reviewer:** Subagent 20261010_93
**Scope:** READ-ONLY analysis of stream_to_vllm, forward_to_vllm, and chat_completions
**Constraint:** No code modified. No sandbox run. No proxy restart.

---

## A. Executive Summary

The current design interleaves Goose-path protections (loop detection, retries, continuation, seam dedup, ctxgate_meta) with fallback-path passthrough inside two shared functions (stream_to_vllm, forward_to_vllm). Every "if _no_continue:" branch is paid by Goose on every request. The fix is a clean function-level split: two new minimal passthrough functions (stream_to_vllm_passthrough, forward_to_vllm_passthrough) dispatched from chat_completions based on _client_cap is not None. After the split, Goose pays **zero** cost for fallback awareness. The fallback path eliminates all loop-detection, retry, continuation, seam-dedup, and ctxgate_meta overhead — a measured 4s+ reduction on a 0.2s request (reasoning_overflow retry case). No Goose-path behavior changes. All protections remain intact and reachable only on the Goose path.

---

## B. Section 1 — Inventory of Mixed-Path Checks

### 1.1. In stream_to_vllm (lines ~4800-5600)

| Line | Condition | Protects | Goose pays? |
|------|-----------|----------|-------------|
| 4800 | _no_continue = vllm_body.pop("_ctxgate_no_continue", False) | Flag extraction | YES (pop on every Goose request) |
| 4898 | if _no_continue: | Initial budget check (MIN_OUTPUT vs 1) | YES (branch evaluated) |
| 5159 | if _no_continue and finish_reason == "length": | Stops continuation after first segment | YES (evaluated on every length finish) |
| 5441 | if _no_continue and finish_reason == "length": | Stops continuation in retry loop | YES (evaluated on every retry) |

**Loop detection in stream_to_vllm (Goose-only logic, but in the shared function):**

| Line | Code | Purpose |
|------|------|---------|
| 5027-5031 | _detect_loop(reasoning_tail) -> loop_period = lp; loop_in_reasoning = True | Periodic loop detection in reasoning stream |
| 5035-5038 | _detect_loop(content_tail) -> loop_period = lp; loop_in_content = True | Periodic loop detection in content stream |
| 5042-5044 | if not loop_period and reasoning_chars > MAX_REASONING_TOKENS * 3 and not full_content and not tc_accum.count(): | Reasoning budget backstop |
| 5125-5154 | exit_reason = "reasoning_overflow" + non-thinking retry block | Reasoning overflow retry |
| 5229-5235 | _tc_markers = ('tool_call', ...) -> reasoning_overflow = True | Tool-call-in-reasoning detection |
| 5238-5261 | if (loop_in_reasoning or reasoning_overflow) and loop_retries < LOOP_RETRIES: | Loop recovery retry (full re-stream) |
| 5386-5402 | _detect_loop calls in retry inner loop | Loop detection during retry stream |

**Other Goose-only mechanisms in stream_to_vllm:**

| Line | Mechanism |
|------|-----------|
| 4821-4824 | Loop state variables initialization |
| 4830-4840 | ToolCallAccumulator initialization |
| 4845-4860 | _seam_resolve / _flush_seam (seam dedup) |
| 4865-4895 | _emit_ctxgate_final (ctxgate metadata emission) |
| 5090-5120 | Tool-call completeness check + unbounded replace-loop guard |
| 5159-5175 | Continuation state machine (main loop) |
| 5260-5310 | Retry stream (full re-POST with modified params) |
| 5483-5484 | empty_after_retry classification |
| 5488-5492 | Exit reason reclassification (loop/overflow) |
| 5549, 5560 | _stream_truncated computation |

### 1.2. In forward_to_vllm (lines ~4450-4760)

| Line | Condition | Protects | Goose pays? |
|------|-----------|----------|-------------|
| 4456 | _no_continue = vllm_body.pop("_ctxgate_no_continue", False) | Flag extraction | YES |
| 4457 | _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")} | Strip internal keys | YES (dict comprehension on every request) |
| 4470 | if _no_continue: | MIN_OUTPUT vs 1 budget check | YES |
| 4613 | if _no_continue: | Stops continuation loop | YES |
| 4615 | exit_reason = "client_capped" | Exit reason classification | YES (string comparison) |

**Goose-only mechanisms in forward_to_vllm:**

| Line | Mechanism |
|------|-----------|
| 4485, 4509 | _send_body filtered dict (strip _ctxgate_*) |
| 4530-4560 | 400 context/max_tokens re-shrink + retry |
| 4570-4600 | Tool-call sanitization + unbounded replace-loop guard |
| 4613-4654 | Continuation loop with reasoning_overflow classification |
| 4660-4700 | ctxgate_meta emission + metrics |

### 1.3. Mechanisms that fire on the fallback path but should NOT

Evidence: a capped fallback request triggered BOTH loop detection AND a reasoning retry (adding 4s+ to a 0.2s request).

| Mechanism | Call Site (line) | Should fire on fallback? |
|-----------|-----------------|------------------------|
| _detect_loop (main loop) | 5027-5038 | NO |
| _detect_loop (retry loop) | 5386-5402 | NO |
| Reasoning budget backstop | 5042-5044 | NO |
| reasoning_overflow retry | 5125-5154 | NO |
| Tool-call-in-reasoning retry | 5229-5235 | NO |
| Loop recovery retry (full re-stream) | 5238-5261 | NO |
| empty_after_retry classification | 5483-5484 | NO |
| Unbounded replace-loop guard | 5090-5120 | NO |
| Tool-call accumulator finalization | 5090-5120, 5460-5480 | NO |
| Seam dedup (_seam_resolve/_flush_seam) | 4845-4860, 5560-5570 | NO |
| ctxgate_meta emission | 4865-4895, 5560-5570 | NO |
| Continuation loop (main) | 5159-5175 | NO (should stop at first length) |
| Continuation loop (retry) | 5441-5443 | NO |

### 1.4. Mechanisms that SHOULD remain on the fallback path

| Mechanism | Currently Present? | Where |
|-----------|-------------------|-------|
| Forwarding max_tokens verbatim (capped) | YES | chat_completions line 4169: max_tokens = min(_budget, _client_cap) |
| Forwarding min_tokens | YES | chat_completions line ~4227: vllm_body["min_tokens"] = min(_min_tok, max_tokens) |
| Forwarding ignore_eos | YES | chat_completions line ~4229: vllm_body["ignore_eos"] = True |
| Forwarding return_token_ids | PARTIAL | Not explicitly forwarded in current code; would need addition to passthrough |
| Forwarding stream_options | YES | chat_completions line ~4231: vllm_body["stream_options"] = {"include_usage": True} |
| Forwarding tools | YES | chat_completions line ~4233: vllm_body["tools"] = body["tools"] |
| Forwarding tool_choice | YES | chat_completions line ~4235: vllm_body["tool_choice"] = body["tool_choice"] |
| Forwarding temperature | YES | chat_completions line ~4210-4220 |
| Forwarding presence_penalty | YES | chat_completions line ~4222 |
| Forwarding repetition_detection | YES | chat_completions line ~4224 |
| Forwarding thinking_token_budget | YES | chat_completions line ~4226 |
| Usage tracking (_track_session_tokens) | YES | In both stream and forward functions |
| Metrics (_record_call) | YES | In both stream and forward functions |
| HTTP 500/503 transient retry once | YES | forward_to_vllm line ~4480-4490; stream_to_vllm has breaker |
| HTTP 400 passthrough (no shrink) | PARTIAL | Currently does shrink+retry; passthrough should just pass through |
| Client disconnect handling | YES | Via asyncio.CancelledError / stream cancellation |

### 1.5. Fallback path distinction in chat_completions

**Grep output:**
```
3860:        _goose_id_header = (
3867:        _client_cap = None
3868:        if not _goose_id_header:
3871:                _client_cap = _cm
3873:        if _goose_id_header:
3875:                "id": _goose_id_header,
3877:                    GOOSE_SESSION_UUID_NAMESPACE, _goose_id_header)),
3878:                "name": _goose_id_header,
3884:                _info = await _get_goose_session_info(_goose_id_header)
3887:                        "name": _info.get("name") or _goose_id_header,
3920:                 _client_cap)
4152:        if _client_cap is not None:
4169:            max_tokens = min(_budget, _client_cap)
4201:            "_ctxgate_no_continue": _client_cap is not None,
4226:        if _client_cap is not None:
4456:        _no_continue = vllm_body.pop("_ctxgate_no_continue", False)
4800:        _no_continue = vllm_body.pop("_ctxgate_no_continue", False)
```

**SINGLE decision point:** Line 3867-3871. The variable _client_cap is set to the client's max_tokens value ONLY when _goose_id_header is empty (i.e., not a Goose request). This is the sole gate. The dispatch at lines 4237-4240:

```python
if stream:
    result = await stream_to_vllm(vllm_body, input_tokens, session_key)
else:
    result = await forward_to_vllm(vllm_body, input_tokens, session_key)
```

This is where the path split should be inserted. The condition is: **_client_cap is not None** -> passthrough; **_client_cap is None** -> Goose path.


---

## C. Section 2 — Proposed Function-Level Split

### 2.1. New function signatures

```python
async def stream_to_vllm_passthrough(vllm_body: dict, input_tokens: int, session_key: str) -> StreamingResponse:
    """Raw SSE passthrough for non-Goose (client-capped) clients.

    One POST to vLLM. No loop detection, no retries, no continuation,
    no ctxgate metadata, no seam dedup, no tool-call accumulator.
    Forwards vLLM's SSE stream verbatim so token_ids and any
    vendor-specific fields survive. Tracks usage for metrics.

    Args:
        vllm_body: Fully constructed vLLM request body (max_tokens
            already capped by chat_completions). All client parameters
            (tools, tool_choice, temperature, min_tokens, ignore_eos,
            stream_options, etc.) are forwarded as-is.
        input_tokens: Pre-computed input token count.
        session_key: Session identifier for metrics tracking.

    Returns:
        StreamingResponse with vLLM's SSE stream passed through
        verbatim. On upstream 4xx/5xx, emits a single error SSE
        chunk + [DONE].
    """
```

```python
async def forward_to_vllm_passthrough(vllm_body: dict, input_tokens: int, session_key: str) -> JSONResponse:
    """Non-streaming raw passthrough for non-Goose (client-capped) clients.

    One POST to vLLM. Same rules as stream_to_vllm_passthrough but
    returns the parsed JSON response directly. No loop detection,
    no retries, no continuation, no ctxgate metadata.

    Args:
        vllm_body: Fully constructed vLLM request body.
        input_tokens: Pre-computed input token count.
        session_key: Session identifier for metrics tracking.

    Returns:
        JSONResponse with vLLM's JSON response verbatim. On upstream
        4xx/5xx, passes through the error status and body.
    """
```

**Field forwarding rules:**
- **ALL fields** in vllm_body are forwarded to vLLM as-is (the body is already built by chat_completions with the client's parameters)
- **Enforced by proxy:** max_tokens is already capped in chat_completions before the call (line 4169)
- **No internal keys** remain: since _ctxgate_no_continue is no longer added to the body, no filtering is needed

**Response shape:**
- Stream: vLLM's SSE stream passed through byte-for-byte (including [DONE])
- Non-stream: vLLM's JSON response verbatim

**Error handling:**
- Upstream 4xx: pass through status code and body (no shrink, no retry)
- Upstream 5xx: pass through status code and body (no retry)
- Timeout: 504 with error body

**Usage tracking:**
- _track_session_tokens(session_key, 0, output_tokens, count_req=False)
- _record_call(session_key, input_tokens, output_tokens, "ok"/"error", VLLM_MODEL, stream, ...)
- metrics["tokens_out_total"] += output_tokens
- metrics["requests_ok"] += 1 or metrics["requests_error"] += 1

**Logging:**
- One INFO line: log.info("PASSTHROUGH session=%s in=%d out=%d wall=%.2fs", session_key, input_tokens, output_tokens, wall_time)

### 2.2. Dispatch code in chat_completions

Replace lines 4237-4240:

```python
        _t0 = time.monotonic()
        if _client_cap is not None:
            # Fallback path: raw passthrough, no Goose protections
            if stream:
                result = await stream_to_vllm_passthrough(vllm_body, input_tokens, session_key)
            else:
                result = await forward_to_vllm_passthrough(vllm_body, input_tokens, session_key)
        else:
            # Goose path: full protections (loop detection, retries, continuation)
            if stream:
                result = await stream_to_vllm(vllm_body, input_tokens, session_key)
            else:
                result = await forward_to_vllm(vllm_body, input_tokens, session_key)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: vllm_call %.0fms (in=%d stream=%s)", _dt, input_tokens, stream)
        return result
```

**Condition:** _client_cap is not None -> passthrough. This is equivalent to not _goose_id_header (a client is "capped" iff it is not Goose and provided a positive integer max_tokens).

### 2.3. Everything that can be DELETED

**Grep confirmation:**
```
4201:            "_ctxgate_no_continue": _client_cap is not None,
4456:        _no_continue = vllm_body.pop("_ctxgate_no_continue", False)
4457:        _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")}
4470:        if _no_continue:
4485:            _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")}
4509:                _send_body = {k: v for k, v in vllm_body.items() if not k.startswith("_ctxgate_")}
4613:            if _no_continue:
4615:                exit_reason = "client_capped"
4800:        _no_continue = vllm_body.pop("_ctxgate_no_continue", False)
4898:            if _no_continue:
4939:                    _send = {k: v for k, v in current_body.items() if not k.startswith("_ctxgate_")}
5159:                if _no_continue and finish_reason == "length":
5161:                    exit_reason = "client_capped"
5280:                        _send = {k: v for k, v in current_body.items() if not k.startswith("_ctxgate_")}
5441:                    if _no_continue and finish_reason == "length":
5443:                        exit_reason = "client_capped"
```

**Deletion list:**

| Item | Lines | Rationale |
|------|-------|-----------|
| "_ctxgate_no_continue": _client_cap is not None in vllm_body construction | 4201 | No longer needed; dispatch happens in chat_completions |
| _no_continue = vllm_body.pop(...) in forward_to_vllm | 4456 | Function is now Goose-only |
| _send_body = {k: v ... if not k.startswith("_ctxgate_")} in forward_to_vllm | 4457, 4485, 4509 | No _ctxgate_* keys remain in vllm_body |
| if _no_continue: in forward_to_vllm | 4470 | Goose always has MIN_OUTPUT check |
| if _no_continue: + exit_reason = "client_capped" in forward_to_vllm | 4613-4615 | No client-capped requests reach this function |
| _no_continue = vllm_body.pop(...) in stream_to_vllm | 4800 | Function is now Goose-only |
| if _no_continue: in stream_to_vllm (initial budget) | 4898 | Goose always has MIN_OUTPUT check |
| _send = {k: v ... if not k.startswith("_ctxgate_")} in stream_to_vllm | 4939, 5280 | No _ctxgate_* keys remain |
| if _no_continue and finish_reason == "length": in stream_to_vllm (main) | 5159-5161 | No client-capped requests reach this function |
| if _no_continue and finish_reason == "length": in stream_to_vllm (retry) | 5441-5443 | No client-capped requests reach this function |
| exit_reason = "client_capped" branches | 4615, 5161, 5443 | Unreachable after split |

**Net deletion:** ~15 lines of branching logic + 5 dict-filtering comprehensions removed from the Goose hot path.

### 2.4. Everything that must remain UNCHANGED in the Goose path

| Mechanism | Location (post-split) | Reachable only on Goose? |
|-----------|----------------------|------------------------|
| _detect_loop (main loop) | stream_to_vllm ~5027-5038 | YES |
| _detect_loop (retry loop) | stream_to_vllm ~5386-5402 | YES |
| Reasoning budget backstop | stream_to_vllm ~5042-5044 | YES |
| reasoning_overflow retry | stream_to_vllm ~5125-5154 | YES |
| Tool-call-in-reasoning retry | stream_to_vllm ~5229-5235 | YES |
| Loop recovery retry | stream_to_vllm ~5238-5261 | YES |
| empty_after_retry classification | stream_to_vllm ~5483-5484 | YES |
| Unbounded replace-loop guard | stream_to_vllm ~5090-5120; forward_to_vllm ~4570-4600 | YES |
| Tool-call accumulator | stream_to_vllm ~4830; forward_to_vllm ~4570 | YES |
| Seam dedup | stream_to_vllm ~4845-4860, ~5560-5570 | YES |
| Continuation loop | stream_to_vllm ~5159-5200; forward_to_vllm ~4613-4660 | YES |
| ctxgate_meta emission | stream_to_vllm ~4865-4895, ~5560; forward_to_vllm ~4660-4700 | YES |
| MIN_OUTPUT floor | stream_to_vllm ~4898 (else branch); forward_to_vllm ~4470 (else branch) | YES |
| emergency_shrink | stream_to_vllm ~4940; forward_to_vllm ~4530 | YES |

All of these are now in functions (stream_to_vllm, forward_to_vllm) that are **only called when _client_cap is None** (i.e., Goose requests). No fallback request will ever enter these functions.


---

## D. Section 3 — SOLID Review

### 3.1. Single Responsibility: **FAIL -> PASS after split**

**Current:** stream_to_vllm and forward_to_vllm each have TWO responsibilities: (1) Goose session management (loop detection, retries, continuation, seam dedup, ctxgate metadata) and (2) raw passthrough for client-capped requests. The _no_continue flag is a runtime switch that changes the function's behavior mid-execution.

**After split:** Each function has ONE responsibility. stream_to_vllm_passthrough = raw SSE forwarding. stream_to_vllm = Goose session management. Clear, testable, independently evolvable.

### 3.2. Open/Closed: **FAIL -> PASS after split**

**Current:** Adding a new client type (e.g., a "benchmark" mode with different retry semantics) requires modifying the shared functions and adding yet another if branch. The functions are closed to extension.

**After split:** New client modes get their own function. The Goose path and passthrough path are independently extensible without touching each other.

### 3.3. Liskov Substitution: **FAIL**

**Current:** stream_to_vllm does NOT behave as a uniform "stream to vLLM" function. When _no_continue=True, it exhibits fundamentally different behavior (no continuation, no loop detection, no seam dedup, no ctxgate meta). A caller cannot substitute one mode for the other expecting the same contract. The function violates LSP because the "passthrough" variant is not a true superset/subset of the "Goose" variant — it's a different algorithm entirely.

**After split:** Each function has a single, consistent contract. LSP is restored.

### 3.4. Interface Segregation: **FAIL -> PASS after split**

**Current:** The vllm_body dict carries an internal flag (_ctxgate_no_continue) that forces every consumer to filter it out (_send_body / _send comprehensions). The interface (the body dict) is polluted with implementation details that the vLLM server doesn't understand.

**After split:** The passthrough functions receive a clean body with no internal keys. The Goose functions also receive a clean body. No filtering needed. The interface is minimal.

### 3.5. Dependency Inversion: **NOTE**

**Current:** The dependency direction is inverted at the dispatch level. chat_completions knows about both paths but the shared functions contain the branching logic. The high-level policy (which path to take) is embedded in the low-level implementation.

**After split:** The high-level policy (_client_cap is not None) lives in chat_completions (the orchestrator). The low-level functions are simple and depend only on their inputs. Dependency inversion is correct: the orchestrator depends on abstractions (function signatures), not on internal branching.

---

## E. Section 4 — Risk / Regression Check

### 4.1. Does ANY behavior change for the Goose path?

**NO.** The Goose path (_client_cap is None) calls the same stream_to_vllm / forward_to_vllm functions with the same parameters. The only changes are:
1. Removal of _no_continue pop (line 4800/4456) — the flag was always False for Goose, so removing the pop changes nothing.
2. Removal of if _no_continue: branches — these were always taken as the else branch for Goose, so behavior is identical.
3. Removal of _send_body / _send filtering — since no _ctxgate_* keys exist in the Goose body (the flag was the only one, and it's being removed from the body construction at line 4201), the filtered dict is identical to the unfiltered dict.

**Zero behavioral change for Goose.**

### 4.2. Does the fallback path's performance improve?

**YES, significantly.**

Current fallback path (capped client) pays for:
- _no_continue pop: negligible
- _send_body dict comprehension (3x in forward, 2x in stream): ~1-5 us each
- if _no_continue: branch evaluations (4x): negligible
- **Loop detection** (_detect_loop on every LOOP_CHECK_EVERY chars): **~50-200 us per check** x many checks per request
- **Reasoning overflow retry**: **4+ seconds** (full re-POST to vLLM with modified params)
- **Seam dedup**: string comparison on 100-char tail
- **ctxgate_meta emission**: JSON construction
- **Tool-call accumulator**: state tracking per chunk

The dominant cost is the **reasoning_overflow retry** (4s+ on a 0.2s request). This is eliminated entirely. Secondary savings: loop detection checks (~1-5ms total per request), seam dedup (~0.1ms), ctxgate meta (~0.1ms).

**Expected improvement:** 4s+ -> 0.2s for the pathological case. ~1-5ms savings for normal cases.

### 4.3. Shared globals whose update semantics change?

| Global | Change? |
|--------|---------|
| metrics dict | **No semantic change.** Passthrough functions update the same counters (requests_ok, requests_error, tokens_out_total, prompt_tokens_total, cached_tokens_total). The output_truncated_by_reason and output_continuations_total counters will simply not increment for passthrough requests (correct — they don't continue). |
| session_seeds deque | **No change.** Only populated in chat_completions before the dispatch. |
| session_compactions | **No change.** Only accessed in chat_completions. |
| _vllm_client (httpx.AsyncClient) | **No change.** Shared by all functions. |
| _vllm_breaker (circuit breaker) | **NOTE:** Currently only the Goose path calls _check_vllm_breaker() and _vllm_breaker.record_success()/record_failure(). The passthrough functions should ALSO record success/failure to keep the breaker accurate. This is a minor addition, not a semantic change. |
| sqlite_conn / sqlite_lock | **No change.** Only accessed in _get_goose_session_info. |

### 4.4. Edge cases

**Client-capped request that gets upstream 500:**
- Passthrough: pass through 500 status + body to client. Record metrics["requests_error"] += 1. No retry.
- Current behavior: also passes through (the 500/503 retry in forward_to_vllm is for Goose; in stream_to_vllm the breaker handles it). **No change in observable behavior** for the client.

**Client-capped request that gets upstream 400:**
- Passthrough: pass through 400 status + body. No shrink, no retry.
- Current behavior: the 400 context/max_tokens shrink+retry fires (lines 4530-4560 in forward, 4940 in stream). **This IS a behavior change** — the passthrough will NOT shrink. This is CORRECT: the client set their own max_tokens, so if vLLM rejects it, the client should know. The shrink was a Goose-specific recovery.

**Client-capped request where the model hits exactly max_tokens:**
- Passthrough: vLLM returns finish_reason: "length". The passthrough forwards this verbatim. No continuation. No retry. Client sees the truncated response.
- Current behavior: same (the _no_continue flag stops continuation). **No change.**

**Client disconnect mid-stream:**
- Passthrough: the async with client.stream(...) context manager is cancelled. The generator is GC'd. No cleanup needed beyond what httpx does.
- Current behavior: same mechanism. **No change.**

---

## F. Recommended Implementation Order

1. **Add stream_to_vllm_passthrough** — new function, ~40 lines. One POST, stream back, track usage, log one line. Handle 4xx/5xx passthrough.

2. **Add forward_to_vllm_passthrough** — new function, ~30 lines. One POST, return JSON, track usage, log one line. Handle 4xx/5xx passthrough.

3. **Modify chat_completions dispatch** — replace the 2-line dispatch with the 8-line conditional (Section 2.2). Remove "_ctxgate_no_continue" from vllm_body construction (line 4201).

4. **Clean up stream_to_vllm** — remove _no_continue pop (line 4800), remove all if _no_continue: branches (lines 4898, 5159, 5441), remove _send filtering (lines 4939, 5280). The function becomes purely Goose.

5. **Clean up forward_to_vllm** — remove _no_continue pop (line 4456), remove all if _no_continue: branches (lines 4470, 4613), remove _send_body filtering (lines 4457, 4485, 4509). The function becomes purely Goose.

6. **Add circuit breaker recording to passthrough functions** — _vllm_breaker.record_success() / record_failure() on the HTTP call.

7. **Test:**
   - Unit test: passthrough stream returns vLLM SSE verbatim
   - Unit test: passthrough non-stream returns vLLM JSON verbatim
   - Unit test: passthrough 400 passes through (no shrink)
   - Unit test: passthrough 500 passes through (no retry)
   - Integration test: Goose request still gets loop detection, continuation, ctxgate_meta
   - Integration test: Fallback request gets NO loop detection, NO continuation, NO ctxgate_meta
   - Performance test: measure wall-time for a capped request that triggers reasoning_overflow (should drop from 4s+ to <1s)

8. **Deploy:** Restart proxy (human action: systemctl --user restart ctxgate-proxy).

---

## G. Open Questions

1. **return_token_ids:** The current code does NOT explicitly forward this field. Should the passthrough add it? If the client sends it, it should be forwarded. Confirm: is this in the client body and just not being copied? (Likely yes — the vllm_body is built from scratch, not copied from the client body.)

2. **Circuit breaker on passthrough:** Should passthrough failures trip the breaker? If a client-capped request gets a 500, should that count toward the breaker's failure threshold? Recommendation: YES (it's still a vLLM failure), but with a lower weight than Goose failures.

3. **Metrics granularity:** Should passthrough requests have a separate metric counter (e.g., metrics["passthrough_requests"]) for observability? Recommendation: YES, additive.

4. **stream_options.include_usage:** The current code always sets this for streaming. The passthrough should also set it (so usage is available in the final chunk). Confirm this is desired.

5. **Backward compatibility:** If any external tooling parses the ctxgate field in the SSE stream, removing it from passthrough responses is a breaking change. Confirm: is the ctxgate field consumed by any non-Goose client? (Likely no — it's a Goose-specific extension.)

---

ARCH-REVIEW COMPLETE — NO CODE CHANGED

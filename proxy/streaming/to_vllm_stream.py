"""Stream (SSE) requests to vLLM with context management.

TODO(I3): This 809-line function should be decomposed into helper functions.
The nested closures (_err_gen, generate, _seam_resolve, _flush_seam,
_emit_ctxgate_final) capture outer-scope variables and cannot be extracted
without changing behavior.
"""
import logging
import asyncio
import json
import time
import httpx
from proxy.streaming.toolcall import ToolCallAccumulator
from proxy.window.cut import _parse_vllm_actual_tokens, _hard_cut_to

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


async def stream_to_vllm(vllm_body: dict, input_tokens: int, session_key: str, live_prompt_hash: str = ""):
    # TODO(I4): replace with AppContext
    from proxy.app import (metrics, _vllm_client, VLLM_URL, VLLM_MODEL,
                           CTXGATE_MAX_TOTAL_OUTPUT, MAX_OUTPUT, MIN_OUTPUT, MAX_INPUT,
                           MAX_CONTEXT, SAFETY_MARGIN,
                           _output_budget, _strip_proxy_metadata, _validate_outgoing_payload,
                           _check_vllm_breaker, _note_tok, _emergency_shrink, _recut_to,
                           count_messages_tokens, estimate_full_payload_tokens, _trim_target,
                           _is_context_length_error, _record_call, CTXGATE_MIN_CONTINUATION_OUTPUT,
                           RETRY_PRESENCE_PENALTY, ContextCapacityError,
                           WALL_CLOCK_MAX, ctxgate_meta, BLOCK_UNBOUNDED_LOOPS,
                           LOOP_CHECK_EVERY, LOOP_RETRIES, LOOP_TAIL, MAX_CONTINUATIONS,
                           MAX_REASONING_TOKENS, RETRY_TEMPERATURE, RETRY_TOP_P,
                           SSE_HEARTBEAT_INTERVAL, _check_unicode_escapes, _detect_loop,
                           _lines_with_heartbeat, _sse_content, _track_session_tokens,
                           _unbounded_replace_loop, _vllm_breaker, calibrator)
    from starlette.responses import StreamingResponse
    global metrics
    # Phase 08: Unified validation before sending
    vllm_body["messages"] = _strip_proxy_metadata(vllm_body.get("messages", []))
    _vok, _verr = _validate_outgoing_payload(vllm_body, live_prompt_hash)
    if not _vok:
        metrics["requests_error"] += 1
        log.error("Outgoing payload validation failed (stream): %s (session=%s)", _verr, session_key)
        async def _err_gen():
            yield "data: " + json.dumps({"error": {"type": _verr, "message": f"Outgoing payload validation failed: {_verr}"}}) + chr(10)*2
            yield "data: [DONE]" + chr(10)*2
        return StreamingResponse(_err_gen(), media_type="text/event-stream")
    async def generate():
        global metrics
        nonlocal input_tokens
        # Content is forwarded immediately. The only text ever held back is
        # the first SEAM_WINDOW chars of a continuation segment (to trim overlap
        # with what the client already has), and it is flushed on every exit path.
        SEAM_WINDOW = 120
        total_output_tokens = 0
        total_cached_tokens = 0
        actual_prompt_tokens = 0
        full_content = ""
        seam_hold = ""
        seam_active = False
        continuation_count = 0
        current_body = dict(vllm_body)
        finish_reason = "stop"
        exit_reason = "ok"
        stream_id = "gen"
        reasoning_tail = ""
        content_tail = ""
        reasoning_chars = 0
        reasoning_full = ""
        loop_check_acc = 0
        loop_retries = 0
        loop_period = 0
        loop_in_reasoning = False
        loop_in_content = False
        reasoning_overflow = False
        reasoning_chars_first = 0  # O2: preserve first-attempt reasoning chars
        notice = ""
        # --- ToolCallAccumulator replaces the old seg_tool_calls_seen boolean ---
        tc_accum = ToolCallAccumulator()
        tc_emitted_count = 0
        tc_buffer: list = []  # buffered tool-call chunks (flushed after safety check)
        loop_intentional = False
        shrink_retried = False
        _bytes_sent = False  # Phase 08: set True after first SSE data chunk sent to client

        def _seam_resolve() -> str:
            nonlocal seam_hold, seam_active
            held, seam_hold, seam_active = seam_hold, "", False
            tail = full_content[-100:]
            overlap = 0
            for j in range(min(len(tail), len(held)), 0, -1):
                if held[:j] == tail[-j:]:
                    overlap = j
                    break
            if overlap > 10:
                log.info("Seam dedup: trimmed %d overlapping chars", overlap)
                held = held[overlap:]
            return held

        def _flush_seam():
            nonlocal full_content, seam_active
            if seam_hold:
                out = _seam_resolve()
                if out:
                    full_content += out
                    yield _sse_content(out, stream_id)
            seam_active = False

        def _emit_ctxgate_final(fr: str, reason: str, conts: int, out_toks: int,
                                 tc_complete: bool, tc_emitted_n: int, tc_truncated: bool = False,
                                 truncated_override: bool = False):
            """Build and yield the final ctxgate chunk + [DONE]."""
            meta = ctxgate_meta(
                truncated=truncated_override if truncated_override else (reason not in ("ok", "tool_calls_complete")),
                reason=reason,
                continuations_used=conts,
                total_output_tokens=out_toks,
                tool_calls_complete=tc_complete,
                tool_calls_emitted=tc_emitted_n,
                tool_call_truncated=tc_truncated,
            )
            if reason != "ok":
                meta["ctxgate"]["reasoning_chars"] = reasoning_chars
            final_chunk = {
                "id": stream_id, "object": "chat.completion.chunk", "created": 0,
                "model": VLLM_MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": fr}],
                "ctxgate": meta["ctxgate"],
            }
            if out_toks:
                _pt = actual_prompt_tokens if actual_prompt_tokens > 0 else input_tokens
                # Invariant: cached_tokens must never exceed prompt_tokens.
                _safe_cached = min(total_cached_tokens, _pt)
                final_chunk["usage"] = {
                    "prompt_tokens": _pt,
                    "completion_tokens": out_toks,
                    "total_tokens": _pt + out_toks,
                    "prompt_tokens_details": {"cached_tokens": _safe_cached},
                }
            yield "data: " + json.dumps(final_chunk) + "\n\n"
            yield "data: [DONE]\n\n"

        try:
            client = _vllm_client
            wall_start = time.time()
            # --- Initial request with total output budget guard ---
            remaining = CTXGATE_MAX_TOTAL_OUTPUT
            _budget = _output_budget(input_tokens)
            max_tokens = min(_budget, MAX_OUTPUT, remaining)
            _caller_cap = vllm_body.get("max_tokens") or 0
            if _caller_cap > 0:
                max_tokens = min(max_tokens, _caller_cap)
            if max_tokens < MIN_OUTPUT:
                log.info("Stream: initial output budget %d < MIN_OUTPUT %d - stopping", max_tokens, MIN_OUTPUT)
                exit_reason = "total_output_budget"
                metrics["output_total_budget_exhausted"] += 1
                for x in _flush_seam():
                    yield x
                for _cg in _emit_ctxgate_final("length", exit_reason, 0, total_output_tokens, False, 0):
                    yield _cg
                metrics["requests_error"] += 1
                _record_call(session_key, input_tokens, 0, "budget", VLLM_MODEL, True, "initial budget exhausted")
                return
            current_body["max_tokens"] = max_tokens
            metrics["output_max_tokens_seen"] = max(metrics["output_max_tokens_seen"], max_tokens)

            while True:
                if time.time() - wall_start > WALL_CLOCK_MAX:
                    log.warning("Wall clock %ds exceeded - stopping stream", WALL_CLOCK_MAX)
                    exit_reason = "wall_clock"
                    break
                finish_reason = "stop"
                got_finish = False
                got_done = False
                seg_output_tokens = 0
                interrupted = None
                loop_intentional = False
                tc_buffer = []
                _check_vllm_breaker()
                # === PRE-SEND GATE: hard invariant ===
                _pre_input = estimate_full_payload_tokens(
                    current_body["messages"], current_body.get("tools", []), _note_tok(), session_key
                )
                _pre_total = _pre_input + current_body["max_tokens"]
                if _pre_total > MAX_CONTEXT:
                    log.warning(
                        "Pre-send gate: input=%d + output=%d = %d > MAX_CONTEXT=%d - shrinking (session=%s)",
                        _pre_input, current_body["max_tokens"], _pre_total, MAX_CONTEXT, session_key
                    )
                    _target_input = MAX_CONTEXT - current_body["max_tokens"] - SAFETY_MARGIN
                    if _target_input < 500:
                        _reduced_output = max(100, MAX_CONTEXT - _pre_input - SAFETY_MARGIN)
                        current_body["max_tokens"] = _reduced_output
                        _target_input = MAX_CONTEXT - _reduced_output - SAFETY_MARGIN
                    if _target_input > 0:
                        _recut_to(current_body["messages"], _target_input)
                        _pre_input = estimate_full_payload_tokens(
                            current_body["messages"], current_body.get("tools", []), _note_tok(), session_key
                        )
                        current_body["max_tokens"] = _output_budget(_pre_input)
                    log.info(
                        "Pre-send gate resolved: input=%d + output=%d = %d <= MAX_CONTEXT=%d",
                        _pre_input, current_body["max_tokens"],
                        _pre_input + current_body["max_tokens"], MAX_CONTEXT
                    )
                # === END PRE-SEND GATE ===
                try:
                    async with client.stream("POST", VLLM_URL + "/chat/completions", json=current_body) as resp:
                        if resp.status_code != 200:
                            body_bytes = await resp.aread()
                            _et = body_bytes[:500].decode("utf-8", errors="replace")
                            _is_ctx_err = _is_context_length_error(resp.status_code, _et)
                            if _is_ctx_err and not shrink_retried and not _bytes_sent:
                                # Phase 08: ONE recut retry, only if no bytes sent to client
                                shrink_retried = True
                                log.warning("Stream context-length error - ONE recut retry (no bytes sent): %s", _et[:200])
                                calibrator.bump_margin(session_key)
                                
                                # Parse actual token count from vLLM error for accurate recut target
                                _actual_in, _actual_max = _parse_vllm_actual_tokens(_et)
                                if _actual_in:
                                    _desired_out = min(_actual_max or MIN_OUTPUT, MAX_OUTPUT)
                                    _recut_target = MAX_CONTEXT - SAFETY_MARGIN - _desired_out
                                    log.info("Recut using vLLM actual: input=%d max_tokens=%d target_input=%d",
                                             _actual_in, _desired_out, _recut_target)
                                    # Hard cut: use vLLM's exact count, no estimation
                                    _hard_cut_to(current_body["messages"], _actual_in, _recut_target)
                                else:
                                    _recut_target = _trim_target()
                                    try:
                                        _recut_to(current_body["messages"], _recut_target)
                                    except ContextCapacityError as cce:
                                        for x in _flush_seam():
                                            yield x
                                        for _cg in _emit_ctxgate_final("length", "context_capacity", continuation_count, total_output_tokens, False, 0):
                                            yield _cg
                                        metrics["requests_error"] += 1
                                        _record_call(session_key, input_tokens, total_output_tokens, "context_capacity", VLLM_MODEL, True, str(cce)[:200])
                                        return

                                # Re-estimate for tracking
                                input_tokens = estimate_full_payload_tokens(current_body["messages"], current_body.get("tools", []), _note_tok(), session_key)
                                _b2 = _output_budget(input_tokens)
                                current_body["max_tokens"] = min(_b2, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                                # Phase 08: validate recut payload
                                _vok, _verr = _validate_outgoing_payload(current_body, live_prompt_hash)
                                if not _vok:
                                    metrics["requests_error"] += 1
                                    log.error("Stream recut validation failed: %s (session=%s)", _verr, session_key)
                                    _record_call(session_key, input_tokens, total_output_tokens, f"recut_validation_failed:{_verr}", VLLM_MODEL, True, _verr)
                                    yield "data: " + json.dumps({"error": {"type": _verr, "message": f"Recut validation failed: {_verr}"}})  + chr(10)*2
                                    yield "data: [DONE]" + chr(10)*2
                                    return
                                continue
                            elif _is_ctx_err and _bytes_sent:
                                # Phase 08: bytes already sent - cannot recut, emit error event
                                log.warning("Stream context-length error AFTER bytes sent - emitting SSE error (no recut): %s", _et[:200])
                                metrics["requests_error"] += 1
                                _record_call(session_key, input_tokens, total_output_tokens, "context_length_exceeded_mid_stream", VLLM_MODEL, True, _et[:200])
                                for x in _flush_seam():
                                    yield x
                                yield "data: " + json.dumps({"error": {"type": "context_length_exceeded", "message": "Context length exceeded mid-stream. No retry possible after bytes sent."}})  + chr(10)*2
                                yield "data: [DONE]" + chr(10)*2
                                return
                            else:
                                # Non-context errors: pass through
                                metrics["requests_error"] += 1
                                _record_call(session_key, input_tokens, total_output_tokens, "vllm_" + str(resp.status_code), VLLM_MODEL, True, body_bytes[:300].decode("utf-8", errors="replace"))
                                for x in _flush_seam():
                                    yield x
                                yield "data: " + json.dumps({"error": body_bytes[:200].decode("utf-8", errors="replace")})  + chr(10)*2
                                yield "data: [DONE]" + chr(10)*2
                                return
                        async for line in _lines_with_heartbeat(resp, SSE_HEARTBEAT_INTERVAL):
                            if line is None:
                                yield ": hb\n\n"
                                continue
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                got_done = True
                                break
                            try:
                                chunk = json.loads(data_str)
                                stream_id = chunk.get("id") or stream_id
                                usage = chunk.get("usage")
                                if usage:
                                    if usage.get("completion_tokens"):
                                        seg_output_tokens = usage["completion_tokens"]
                                    total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                    _real_pt_s = usage.get("prompt_tokens", 0)
                                    if _real_pt_s > 0:
                                        actual_prompt_tokens = _real_pt_s
                                        calibrator.observe(session_key, input_tokens, _real_pt_s)
                                        log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                                                 input_tokens, _real_pt_s, 100.0*(input_tokens-_real_pt_s)/_real_pt_s,
                                                 calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
                                    usage["prompt_tokens"] = _real_pt_s if _real_pt_s > 0 else input_tokens
                                    usage["total_tokens"] = usage["prompt_tokens"] + (usage.get("completion_tokens") or 0)
                                choices = chunk.get("choices", [])
                                if not choices:
                                    continue
                                fr = choices[0].get("finish_reason")
                                if fr:
                                    finish_reason = fr
                                    got_finish = True
                                delta = choices[0].get("delta", {})
                                reasoning_piece = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                                tool_calls_piece = delta.get("tool_calls")
                                if reasoning_piece:
                                    rc = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"reasoning_content": reasoning_piece}, "finish_reason": None}]}
                                    _bytes_sent = True
                                    yield "data: " + json.dumps(rc) + "\n\n"
                                    reasoning_chars += len(reasoning_piece)
                                    reasoning_full += reasoning_piece
                                    reasoning_tail = (reasoning_tail + reasoning_piece)[-LOOP_TAIL:]
                                    loop_check_acc += len(reasoning_piece)
                                # --- ToolCallAccumulator: track completeness + forward in real-time ---
                                if tool_calls_piece:
                                    tc_accum.add_delta(tool_calls_piece)
                                    tc_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"tool_calls": tool_calls_piece}, "finish_reason": None}]}
                                    tc_buffer.append(tc_chunk)
                                content_piece = delta.get("content", "")
                                if content_piece:
                                    if seam_active:
                                        seam_hold += content_piece
                                        if len(seam_hold) < SEAM_WINDOW:
                                            continue
                                        out = _seam_resolve()
                                    else:
                                        out = content_piece
                                    if out:
                                        full_content += out
                                        content_tail = (content_tail + out)[-LOOP_TAIL:]
                                        loop_check_acc += len(out)
                                        _bytes_sent = True
                                        yield _sse_content(out, stream_id)
                                if loop_check_acc >= LOOP_CHECK_EVERY:
                                    loop_check_acc = 0
                                    if not loop_period:
                                        lp = _detect_loop(reasoning_tail)
                                        if lp:
                                            loop_period = lp
                                            loop_in_reasoning = True
                                            log.warning("Loop in reasoning (period=%d chars=%d) - stopping upstream", lp, reasoning_chars)
                                            loop_intentional = True
                                            break
                                        lp = _detect_loop(content_tail)
                                        if lp:
                                            loop_period = lp
                                            loop_in_content = True
                                            log.warning("Loop in content (period=%d chars=%d) - stopping stream", lp, len(full_content))
                                            loop_intentional = True
                                            break
                                if not loop_period and reasoning_chars > MAX_REASONING_TOKENS * 3 and not full_content and not tc_accum.count():
                                    loop_in_reasoning = True
                                    reasoning_overflow = True
                                    log.warning("Reasoning budget backstop (chars=%d > %d) with no content - treating as loop", reasoning_chars, MAX_REASONING_TOKENS * 3)
                                    loop_intentional = True
                                    break
                            except (json.JSONDecodeError, ValueError):
                                yield "data: " + data_str + "\n\n"
                except httpx.TransportError as e:
                    interrupted = "%s: %s" % (type(e).__name__, str(e)[:150])
                    _vllm_breaker.record_failure()

                # Release any text still held for seam trimming
                for x in _flush_seam():
                    yield x

                if interrupted is None and not got_finish and not got_done and not loop_intentional:
                    interrupted = "upstream closed the stream early"

                if interrupted and full_content and finish_reason == "stop":
                    finish_reason = "length"
                total_output_tokens += seg_output_tokens
                if interrupted is None:
                    _vllm_breaker.record_success()
                else:
                    # Failure already recorded in except; early-close is also a failure
                    pass

                # --- Tool call completeness check ---
                tc_complete = tc_accum.is_complete() if tc_accum.count() > 0 else False
                tc_incomplete = tc_accum.count() > 0 and not tc_complete
                # --- Flush buffered tool calls (after safety check) ---
                if tc_buffer:
                    _unsafe_tc = False
                    if tc_complete and BLOCK_UNBOUNDED_LOOPS:
                        for _idx, _tc in tc_accum._calls.items():
                            if _tc.get("name") == "execute_typescript":
                                try:
                                    _args = json.loads(_tc.get("arguments", "{}"))
                                except Exception:
                                    _args = {}
                                if _unbounded_replace_loop(_args.get("code", "")):
                                    _unsafe_tc = True
                                    break
                    if _unsafe_tc:
                        metrics["unsafe_loop_blocked"] += 1
                        log.warning("Stream: blocked unbounded replace-loop (session=%s)", session_key)
                        tc_buffer.clear()
                        tc_accum = ToolCallAccumulator()
                        _err_text = "BLOCKED: execute_typescript code contains an unbounded while(var.includes(X)) { var = var.replace(X, Y) } loop. Use s.replaceAll(A, B) or s.split(A).join(B) instead. Any loop must have an explicit iteration cap (e.g. for (let i=0;i<1000;i++))."
                        _err_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": 0, "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"content": _err_text}, "finish_reason": None}]}
                        yield "data: " + json.dumps(_err_chunk) + "\n\n"
                        finish_reason = "stop"
                        exit_reason = "unsafe_loop_blocked"
                        break
                    else:
                        if tc_incomplete:
                            tc_buffer.clear()
                            # Do NOT emit. The outer classification will set
                            # tool_call_truncated=true and finish_reason="length".
                        else:
                            for _tc_chunk in tc_buffer:
                                yield "data: " + json.dumps(_tc_chunk) + "\n\n"
                            tc_emitted_count += len(tc_buffer)
                            tc_buffer.clear()
                            if tc_complete:
                                finish_reason = "tool_calls"
                                exit_reason = "tool_calls_complete"
                                break

                if interrupted and not loop_intentional and exit_reason not in ("tool_calls_complete", "unsafe_loop_blocked"):
                    log.warning("Stream interrupted after %d chars: %s", len(full_content), interrupted)
                    if tc_incomplete or not full_content or continuation_count >= MAX_CONTINUATIONS:
                        exit_reason = "interrupted"
                        finish_reason = "length"
                        break
                    finish_reason = "length"
                    await asyncio.sleep(min(2 * (continuation_count + 1), 5))

                # --- Reasoning overflow: length stop with empty content ---
                if finish_reason == "length" and not full_content and not tc_accum.count():
                    if loop_retries < LOOP_RETRIES:
                        loop_retries += 1
                        exit_reason = "reasoning_overflow"
                        metrics["summary_retry_count"] += 1
                        log.info("reasoning_overflow (length, empty content) - non-thinking retry (%d/%d)", loop_retries, LOOP_RETRIES)
                        current_body = dict(vllm_body)
                        current_body["messages"] = list(vllm_body.get("messages", []))
                        _rb = _output_budget(count_messages_tokens(vllm_body.get("messages", [])))
                        current_body["max_tokens"] = min(_rb, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                        current_body["chat_template_kwargs"] = {"enable_thinking": False}
                        current_body["temperature"] = RETRY_TEMPERATURE
                        current_body["top_p"] = RETRY_TOP_P
                        current_body["presence_penalty"] = RETRY_PRESENCE_PENALTY
                        # O2: keep stream_options so vLLM sends usage chunk
                        if "stream_options" not in current_body:
                            current_body["stream_options"] = {"include_usage": True}
                        reasoning_tail = ""
                        content_tail = ""
                        reasoning_chars = 0
                        reasoning_full = ""
                        loop_period = 0
                        loop_check_acc = 0
                        loop_in_reasoning = False
                        loop_in_content = False
                        reasoning_overflow = False
                        tc_accum = ToolCallAccumulator()
                        seam_active = False
                        seam_hold = ""
                        continue
                    else:
                        log.warning("reasoning_overflow but no retries left - stopping")
                        exit_reason = "reasoning_overflow"
                        finish_reason = "length"
                        break

                # --- Continuation state machine ---
                if finish_reason == "length" and continuation_count < MAX_CONTINUATIONS:
                    # Incomplete tool call -> NO continuation
                    if tc_incomplete:
                        log.warning("Incomplete tool call set - NO continuation (truncated)")
                        exit_reason = "tool_call_truncated"
                        metrics["tool_call_truncated"] += 1
                        finish_reason = "length"
                        break
                    # Complete tool call set -> terminate, no continuation
                    if tc_complete:
                        log.info("Complete tool-call set - terminating turn (no continuation)")
                        finish_reason = "tool_calls"
                        exit_reason = "tool_calls_complete"
                        break
                    # Pure text continuation
                    remaining = CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens
                    if remaining < CTXGATE_MIN_CONTINUATION_OUTPUT:
                        log.info("Stream: total output budget exhausted (%d/%d) - stopping", total_output_tokens, CTXGATE_MAX_TOTAL_OUTPUT)
                        exit_reason = "total_output_budget"
                        metrics["output_total_budget_exhausted"] += 1
                        finish_reason = "length"
                        break
                    continuation_count += 1
                    metrics["output_continuations_total"] += 1
                    log.info("vLLM hit max_tokens - auto-continuing (%d/%d, remaining=%d)", continuation_count, MAX_CONTINUATIONS, remaining)
                    cont_messages = list(vllm_body.get("messages", []))
                    cont_messages.append({"role": "assistant", "content": full_content})
                    cont_messages.append({"role": "user", "content": "Continue from exactly where you left off. Do not repeat any content already provided. Resume the next word/sentence/code line."})
                    cont_tokens = count_messages_tokens(cont_messages)
                    _cont_budget = _output_budget(cont_tokens)
                    max_tokens = min(_cont_budget, MAX_OUTPUT, remaining)
                    if cont_tokens > MAX_INPUT or max_tokens < CTXGATE_MIN_CONTINUATION_OUTPUT:
                        log.info("Stream cont: would exceed input budget (%d > %d) or starve output (%d < %d) - stopping", cont_tokens, MAX_INPUT, max_tokens, CTXGATE_MIN_CONTINUATION_OUTPUT)
                        exit_reason = "cont_budget"
                        finish_reason = "length"
                        break
                    current_body = dict(vllm_body)
                    current_body["messages"] = cont_messages
                    current_body["max_tokens"] = max_tokens
                    metrics["output_max_tokens_seen"] = max(metrics["output_max_tokens_seen"], max_tokens)
                    seam_active = bool(full_content)
                    seam_hold = ""
                    continue
                elif finish_reason == "length":
                    log.warning("Max continuations (%d) reached - stopping", MAX_CONTINUATIONS)
                    exit_reason = "max_continuations"
                    finish_reason = "length"
                break

            # O2: save first-attempt reasoning chars before any retry resets them
            if loop_retries == 0 and reasoning_chars > 0:
                reasoning_chars_first = reasoning_chars

            # Empty-response recovery
            if (not notice and not full_content and not tc_accum.count()
                    and finish_reason == "stop" and reasoning_chars > 500
                    and loop_retries < LOOP_RETRIES):
                log.warning(
                    "Empty content after %d reasoning chars (finish=stop) - "
                    "treating as reasoning_overflow for non-thinking retry",
                    reasoning_chars,
                )
                reasoning_overflow = True

            # --- Tool call emitted inside reasoning (no content, no tool_calls) ---
            _tc_markers = ('<tool_call>', '<function=', '</parameter>', '</function>')
            if (not full_content and not tc_accum.count()
                    and finish_reason == "stop"
                    and any(m in reasoning_full for m in _tc_markers)):
                log.warning("tool call emitted inside reasoning (no content, no tool_calls) - non-thinking retry")
                metrics["tool_call_in_reasoning"] += 1
                reasoning_overflow = True

            # Loop recovery
            if (loop_in_reasoning or reasoning_overflow) and loop_retries < LOOP_RETRIES:
                loop_retries += 1
                metrics["summary_retry_count"] += 1
                log.info("Loop in reasoning - non-thinking retry (%d/%d)", loop_retries, LOOP_RETRIES)
                current_body = dict(vllm_body)
                current_body["messages"] = list(vllm_body.get("messages", []))
                _rb2 = _output_budget(count_messages_tokens(vllm_body.get("messages", [])))
                current_body["max_tokens"] = min(_rb2, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                current_body["chat_template_kwargs"] = {"enable_thinking": False}
                current_body["temperature"] = RETRY_TEMPERATURE
                current_body["top_p"] = RETRY_TOP_P
                current_body["presence_penalty"] = RETRY_PRESENCE_PENALTY
                # O2: keep stream_options so vLLM sends usage chunk
                if "stream_options" not in current_body:
                    current_body["stream_options"] = {"include_usage": True}
                reasoning_tail = ""
                content_tail = ""
                reasoning_chars = 0
                reasoning_full = ""
                loop_period = 0
                loop_check_acc = 0
                loop_in_reasoning = False
                loop_in_content = False
                reasoning_overflow = False
                tc_accum = ToolCallAccumulator()
                seam_active = False
                seam_hold = ""
                wall_start = time.time()
                finish_reason = "stop"
                while True:
                    if time.time() - wall_start > WALL_CLOCK_MAX:
                        exit_reason = "wall_clock"
                        break
                    finish_reason = "stop"
                    got_finish = False
                    got_done = False
                    seg_output_tokens = 0
                    interrupted = None
                    tc_buffer = []
                    _check_vllm_breaker()
                    _retry_t0 = time.monotonic()
                    # === PRE-SEND GATE: hard invariant ===
                    _pre_input = estimate_full_payload_tokens(
                        current_body["messages"], current_body.get("tools", []), _note_tok(), session_key
                    )
                    _pre_total = _pre_input + current_body["max_tokens"]
                    if _pre_total > MAX_CONTEXT:
                        log.warning(
                            "Pre-send gate: input=%d + output=%d = %d > MAX_CONTEXT=%d - shrinking (session=%s)",
                            _pre_input, current_body["max_tokens"], _pre_total, MAX_CONTEXT, session_key
                        )
                        _target_input = MAX_CONTEXT - current_body["max_tokens"] - SAFETY_MARGIN
                        if _target_input < 500:
                            _reduced_output = max(100, MAX_CONTEXT - _pre_input - SAFETY_MARGIN)
                            current_body["max_tokens"] = _reduced_output
                            _target_input = MAX_CONTEXT - _reduced_output - SAFETY_MARGIN
                        if _target_input > 0:
                            _recut_to(current_body["messages"], _target_input)
                            _pre_input = estimate_full_payload_tokens(
                                current_body["messages"], current_body.get("tools", []), _note_tok(), session_key
                            )
                            current_body["max_tokens"] = _output_budget(_pre_input)
                        log.info(
                            "Pre-send gate resolved: input=%d + output=%d = %d <= MAX_CONTEXT=%d",
                            _pre_input, current_body["max_tokens"],
                            _pre_input + current_body["max_tokens"], MAX_CONTEXT
                        )
                    # === END PRE-SEND GATE ===
                    try:
                        async with client.stream("POST", VLLM_URL + "/chat/completions", json=current_body) as resp:
                            if loop_retries > 0:
                                log.info("CACHE retry_ttft=%.3f", time.monotonic() - _retry_t0)
                            if resp.status_code != 200:
                                body_bytes = await resp.aread()
                                _et = body_bytes[:500].decode("utf-8", errors="replace").lower()
                                if (resp.status_code == 400 and not shrink_retried
                                        and any(p in _et for p in ("context", "max_tokens", "max_completion_tokens", "maximum", "greater than"))):
                                    shrink_retried = True
                                    log.warning("Stream 400 retry (context/max_tokens) - re-shrinking: %s", _et[:200])
                                    calibrator.bump_margin(session_key)
                                    try:
                                        current_body["messages"] = _emergency_shrink(current_body["messages"], int(input_tokens * 0.8))
                                    except ContextCapacityError as cce:
                                        for x in _flush_seam():
                                            yield x
                                        for _cg in _emit_ctxgate_final("length", "context_capacity", continuation_count, total_output_tokens, False, 0):
                                            yield _cg
                                        metrics["requests_error"] += 1
                                        _record_call(session_key, input_tokens, total_output_tokens, "context_capacity", VLLM_MODEL, True, str(cce)[:200])
                                        return
                                    input_tokens = estimate_full_payload_tokens(current_body["messages"], current_body.get("tools", []), _note_tok(), session_key)
                                    _b3 = _output_budget(input_tokens)
                                    current_body["max_tokens"] = min(_b3, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens)
                                    continue
                                metrics["requests_error"] += 1
                                _record_call(session_key, input_tokens, total_output_tokens, "vllm_" + str(resp.status_code), VLLM_MODEL, True, body_bytes[:300].decode("utf-8", errors="replace"))
                                for x in _flush_seam():
                                    yield x
                                yield "data: " + json.dumps({"error": body_bytes[:200].decode("utf-8", errors="replace")}) + "\n\n"
                                yield "data: [DONE]\n\n"
                                return
                            async for line in _lines_with_heartbeat(resp, SSE_HEARTBEAT_INTERVAL):
                                if line is None:
                                    yield ": hb\n\n"
                                    continue
                                if not line.startswith("data: "):
                                    continue
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    got_done = True
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                    stream_id = chunk.get("id") or stream_id
                                    usage = chunk.get("usage")
                                    if usage:
                                        if usage.get("completion_tokens"):
                                            seg_output_tokens = usage["completion_tokens"]
                                        total_cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                                        _real_pt_r = usage.get("prompt_tokens", 0)
                                        if _real_pt_r > 0:
                                            actual_prompt_tokens = _real_pt_r
                                            calibrator.observe(session_key, input_tokens, _real_pt_r)
                                            log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                                                     input_tokens, _real_pt_r, 100.0*(input_tokens-_real_pt_r)/_real_pt_r,
                                                     calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
                                        usage["prompt_tokens"] = _real_pt_r if _real_pt_r > 0 else input_tokens
                                        usage["total_tokens"] = usage["prompt_tokens"] + (usage.get("completion_tokens") or 0)
                                        try:
                                            _ptd = usage.get("prompt_tokens_details") or {}
                                            _ct = _ptd.get("cached_tokens", 0)
                                            _pt = usage.get("prompt_tokens", 0)
                                            if _pt:
                                                log.info("Retry cache: cached=%d/%d prompt_tokens (%.0f%%)", _ct, _pt, 100.0 * _ct / _pt)
                                        except Exception:
                                            pass
                                    choices = chunk.get("choices", [])
                                    if not choices:
                                        continue
                                    fr = choices[0].get("finish_reason")
                                    if fr:
                                        finish_reason = fr
                                        got_finish = True
                                    delta = choices[0].get("delta", {})
                                    reasoning_piece = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                                    tool_calls_piece = delta.get("tool_calls")
                                    if reasoning_piece:
                                        rc = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"reasoning_content": reasoning_piece}, "finish_reason": None}]}
                                        yield "data: " + json.dumps(rc) + "\n\n"
                                        reasoning_chars += len(reasoning_piece)
                                        reasoning_full += reasoning_piece
                                        reasoning_tail = (reasoning_tail + reasoning_piece)[-LOOP_TAIL:]
                                    loop_check_acc += len(reasoning_piece)
                                    if tool_calls_piece:
                                        tc_accum.add_delta(tool_calls_piece)
                                        tc_chunk = {"id": stream_id, "object": "chat.completion.chunk", "created": chunk.get("created", 0), "model": VLLM_MODEL, "choices": [{"index": 0, "delta": {"tool_calls": tool_calls_piece}, "finish_reason": None}]}
                                        tc_buffer.append(tc_chunk)
                                    content_piece = delta.get("content", "")
                                    if content_piece:
                                        full_content += content_piece
                                        content_tail = (content_tail + content_piece)[-LOOP_TAIL:]
                                        loop_check_acc += len(content_piece)
                                        yield _sse_content(content_piece, stream_id)
                                except (json.JSONDecodeError, ValueError):
                                    yield "data: " + data_str + "\n\n"
                    except httpx.TransportError as e:
                        interrupted = "%s: %s" % (type(e).__name__, str(e)[:150])
                        _vllm_breaker.record_failure()
                    for x in _flush_seam():
                        yield x
                    if interrupted is None and not got_finish and not got_done:
                        interrupted = "upstream closed the stream early"
                    total_output_tokens += seg_output_tokens
                    if interrupted and full_content and finish_reason == "stop":
                        finish_reason = "length"
                    if interrupted is None:
                        _vllm_breaker.record_success()
                    else:
                        # Failure already recorded in except
                        pass
                    if loop_check_acc >= LOOP_CHECK_EVERY:
                        loop_check_acc = 0
                        if not loop_period:
                            lp = _detect_loop(content_tail)
                            if lp:
                                loop_period = lp
                                loop_in_content = True
                                log.warning("Retry looped in content (period=%d) - stopping", lp)
                                break
                        if not loop_period:
                            lp = _detect_loop(reasoning_tail)
                            if lp:
                                loop_period = lp
                                loop_in_reasoning = True
                                log.warning("Retry looped in reasoning (period=%d) - stopping", lp)
                                break
                    if not loop_period and reasoning_chars > MAX_REASONING_TOKENS * 3 and not full_content and not tc_accum.count():
                        loop_in_reasoning = True
                        reasoning_overflow = True
                        log.warning("Retry reasoning budget backstop (chars=%d > %d) - stopping", reasoning_chars, MAX_REASONING_TOKENS * 3)
                        break
                    # --- Flush buffered tool calls from retry (after safety check) ---
                    tc_complete_r = tc_accum.is_complete() if tc_accum.count() > 0 else False
                    tc_incomplete_r = tc_accum.count() > 0 and not tc_complete_r
                    if tc_buffer:
                        _unsafe_tc = False
                        if tc_complete_r and BLOCK_UNBOUNDED_LOOPS:
                            for _idx, _tc in tc_accum._calls.items():
                                if _tc.get('name') == 'execute_typescript':
                                    try:
                                        _args = json.loads(_tc.get('arguments', '{}'))
                                    except Exception:
                                        _args = {}
                                    if _unbounded_replace_loop(_args.get('code', '')):
                                        _unsafe_tc = True
                        if _unsafe_tc:
                            metrics['unsafe_loop_blocked'] += 1
                            log.warning('Stream retry: blocked unbounded replace-loop (session=%s)', session_key)
                            tc_buffer.clear()
                            tc_accum = ToolCallAccumulator()
                            _err_text = 'BLOCKED: execute_typescript code contains an unbounded while(var.includes(X)) { var = var.replace(X, Y) } loop. Use s.replaceAll(A, B) or s.split(A).join(B) instead. Any loop must have an explicit iteration cap.'
                            _err_chunk = {'id': stream_id, 'object': 'chat.completion.chunk', 'created': 0, 'model': VLLM_MODEL, 'choices': [{'index': 0, 'delta': {'content': _err_text}, 'finish_reason': None}]}
                            yield 'data: ' + json.dumps(_err_chunk) + '\n\n'
                            finish_reason = 'stop'
                            exit_reason = 'unsafe_loop_blocked'
                        else:
                            if tc_incomplete_r:
                                tc_buffer.clear()
                                # Do NOT emit.
                            else:
                                for _tc_chunk in tc_buffer:
                                    yield 'data: ' + json.dumps(_tc_chunk) + '\n\n'
                                tc_emitted_count += len(tc_buffer)
                                tc_buffer.clear()
                                if tc_complete_r:
                                    finish_reason = 'tool_calls'
                                    exit_reason = 'tool_calls_complete'
                    if interrupted and exit_reason not in ("tool_calls_complete", "unsafe_loop_blocked"):
                        log.warning("Retry stream interrupted after %d chars: %s", len(full_content), interrupted)
                        exit_reason = "interrupted"
                        finish_reason = "length"
                    break
                if exit_reason not in ("interrupted", "tool_calls_complete", "unsafe_loop_blocked"):
                    if loop_in_content:
                        exit_reason = "loop"
                        finish_reason = "length"
                    elif tc_accum.is_complete() and tc_accum.count() > 0:
                        exit_reason = "tool_calls_complete"
                        finish_reason = "tool_calls"
                    elif loop_in_reasoning or reasoning_overflow:
                        exit_reason = "reasoning_overflow"
                        finish_reason = "length"
                    elif finish_reason == "length":
                        # O3: retry segment hit max_tokens without completing
                        exit_reason = "retry_length"
                    else:
                        exit_reason = "loop_recovered"

            # Classify empty responses
            if exit_reason in ("ok", "loop_recovered") and not full_content and not tc_accum.count():
                exit_reason = "empty"

            # WI-10: Unicode escape diagnostics
            if tc_accum.count() > 0:
                _ue_count = _check_unicode_escapes(tc_accum)
                if _ue_count > 0:
                    metrics["tool_args_unicode_escape"] += _ue_count
                    log.warning("Unicode escapes in tool args (%d occurrences, session=%s)", _ue_count, session_key[:16])

            # --- Empty after retry: no synthetic content, truthful exit reason ---
            if (loop_retries > 0
                    and exit_reason not in ("unsafe_loop_blocked", "interrupted", "tool_calls_complete")
                    and not full_content and not tc_accum.count()):
                finish_reason = "stop"
                exit_reason = "empty_after_retry"
                metrics["empty_after_retry"] += 1
                log.warning("Empty response after retry (session=%s) - no content, no tool calls", session_key[:16])

            # --- Phase 1b: fix exit_reason for loop/overflow recovery ---
            if loop_in_content:
                exit_reason = "content_loop"
            elif loop_in_reasoning:
                exit_reason = "reasoning_loop"
            elif exit_reason == "reasoning_overflow" and not loop_in_reasoning and not loop_in_content:
                if full_content or tc_accum.count():
                    exit_reason = "ok"
                else:
                    exit_reason = "reasoning_loop"

            # --- Final ctxgate chunk + [DONE] ---
            tc_complete = tc_accum.is_complete() if tc_accum.count() > 0 else False
            tc_truncated = tc_accum.count() > 0 and not tc_complete
            tc_emitted_n = tc_emitted_count
            if tc_emitted_count == 0:
                tc_complete = False
            if tc_truncated:
                metrics["tool_call_truncated"] += 1
                # O4: per-session tool call truncation visibility
                _tc_names = []
                for _idx, _tc in tc_accum._calls.items():
                    _tc_names.append(_tc.get("name", "?"))
                log.warning("O4 tool_call_truncated session=%s names=%s max_tokens=%d",
                           session_key, _tc_names, current_body.get("max_tokens", "?"))
            if tc_complete:
                metrics["tool_call_complete"] += 1
            # Determine final finish_reason
            if tc_complete and finish_reason != "tool_calls":
                finish_reason = "tool_calls"
            if exit_reason not in ("ok", "unsafe_loop_blocked") and finish_reason == "stop":
                finish_reason = "length"

            # Metrics
            _success_exit = {"ok", "loop_recovered", "tool_calls_complete"}
            if exit_reason in _success_exit:
                metrics["requests_ok"] += 1
            else:
                metrics["requests_error"] += 1
                # Diagnostic breakdown of real errors (additive, stats-only)
                metrics.setdefault("requests_error_by_reason", {})
                metrics["requests_error_by_reason"][exit_reason] = (
                    metrics["requests_error_by_reason"].get(exit_reason, 0) + 1
                )
            _final_pt = actual_prompt_tokens if actual_prompt_tokens > 0 else input_tokens
            metrics["cached_tokens_total"] += total_cached_tokens
            metrics["prompt_tokens_total"] += _final_pt
            from proxy.hotpath.tracking import store_exact_tokens
            store_exact_tokens(session_key, _final_pt)
            if total_output_tokens:
                metrics["tokens_out_total"] += total_output_tokens
                _track_session_tokens(session_key, 0, total_output_tokens, count_req=False)
            if exit_reason not in ("ok", "tool_calls_complete"):
                metrics["output_truncated_total"] += 1
                _rk = exit_reason
                if _rk not in metrics["output_truncated_by_reason"]:
                    metrics["output_truncated_by_reason"][_rk] = 0
                metrics["output_truncated_by_reason"][_rk] += 1

            _record_call(session_key, input_tokens, total_output_tokens,
                         "ok" if exit_reason == "ok" else "error", VLLM_MODEL, True,
                         "" if exit_reason == "ok" else "stream ended: " + exit_reason,
                         cached_tokens=total_cached_tokens)


            _stream_truncated = (exit_reason != "ok" and exit_reason != "tool_calls_complete") or loop_in_content or loop_in_reasoning
            # One concise diagnostic line
            if exit_reason not in ("ok", "tool_calls_complete"):
                log.warning("NS-DIAG session=%s exit=%s finish=%s truncated=%s conts=%d total_out=%d tc_seen=%d tc_complete=%d tc_emitted=%d reasoning_chars=%d reasoning_chars_first=%d",
                           session_key, exit_reason, finish_reason,
                           _stream_truncated, continuation_count, total_output_tokens,
                           tc_accum.count(), tc_complete, tc_emitted_n, reasoning_chars, reasoning_chars_first)

            # Flush any remaining seam text BEFORE the final marker
            for x in _flush_seam():
                yield x

            _stream_truncated = (exit_reason != "ok" and exit_reason != "tool_calls_complete") or loop_in_content or loop_in_reasoning
            for _cg in _emit_ctxgate_final(finish_reason, exit_reason, continuation_count, total_output_tokens, tc_complete, tc_emitted_n, tc_truncated, truncated_override=_stream_truncated):
                yield _cg
        except ContextCapacityError as e:
            metrics["requests_error"] += 1
            log.error("Context capacity error in stream: %s", e)
            _record_call(session_key, input_tokens, total_output_tokens, "context_capacity", VLLM_MODEL, True, str(e)[:200])
            for x in _flush_seam():
                yield x
            for _cg in _emit_ctxgate_final("length", "context_capacity", continuation_count, total_output_tokens, False, 0):
                yield _cg
        except Exception as e:
            metrics["requests_error"] += 1
            log.exception("Stream error: %s", e)
            _record_call(session_key, input_tokens, total_output_tokens, "error", VLLM_MODEL, True, str(e)[:300])
            for x in _flush_seam():
                yield x
            for _cg in _emit_ctxgate_final("length", "error", continuation_count, total_output_tokens, False, 0):
                yield _cg
    return StreamingResponse(generate(), media_type="text/event-stream")

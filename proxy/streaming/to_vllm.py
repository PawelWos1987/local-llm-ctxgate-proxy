"""Forward (non-streaming) requests to vLLM with context management."""
import logging
import asyncio
import json
import time
import httpx
from proxy.window.cut import _parse_vllm_actual_tokens, _hard_cut_to

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


async def forward_to_vllm(vllm_body: dict, input_tokens: int, session_key: str, live_prompt_hash: str = ""):
    # TODO(I4): replace with AppContext
    from proxy.app import (metrics, _vllm_client, CTXGATE_MAX_TOTAL_OUTPUT, MAX_OUTPUT, MIN_OUTPUT,
                           MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN,
                           _output_budget, _strip_proxy_metadata, _validate_outgoing_payload,
                           VLLM_URL, VLLM_MODEL, _is_context_length_error, _trim_target, _recut_to,
                           estimate_full_payload_tokens, _note_tok, count_messages_tokens, _record_call,
                           JSONResponse, explain_status, CTXGATE_MIN_CONTINUATION_OUTPUT,
                           WALL_CLOCK_MAX, ctxgate_meta, BLOCK_UNBOUNDED_LOOPS, MAX_CONTINUATIONS,
                           _classify_truncation, _safe_truncate, _track_session_tokens,
                           _unbounded_replace_loop, calibrator, sanitize_tool_calls,
                           ContextCapacityError)
    global metrics
    try:
        client = _vllm_client
        # --- Total output budget tracking ---
        total_output_tokens = 0
        cont_count = 0
        exit_reason = "ok"
        _attempts = 0
        # --- Initial request with budget guard ---
        remaining = CTXGATE_MAX_TOTAL_OUTPUT
        _budget = _output_budget(input_tokens)
        max_tokens = min(_budget, MAX_OUTPUT, remaining)
        _caller_cap = vllm_body.get("max_tokens") or 0
        if _caller_cap > 0:
            max_tokens = min(max_tokens, _caller_cap)
        if max_tokens < MIN_OUTPUT:
            log.info("Non-stream: output budget %d < MIN_OUTPUT %d - stopping", max_tokens, MIN_OUTPUT)
            return JSONResponse({"error": {"message": "Output budget exhausted", "explanation": explain_status("budget")}}, status_code=413)
        vllm_body["max_tokens"] = max_tokens
        metrics["output_max_tokens_seen"] = max(metrics["output_max_tokens_seen"], max_tokens)
        # Phase 08: Unified validation before sending
        vllm_body["messages"] = _strip_proxy_metadata(vllm_body.get("messages", []))
        _vok, _verr = _validate_outgoing_payload(vllm_body, live_prompt_hash)
        if not _vok:
            metrics["requests_error"] += 1
            log.error("Outgoing payload validation failed: %s (session=%s)", _verr, session_key)
            return JSONResponse(
                {"error": {"type": _verr,
                 "message": f"Outgoing payload validation failed: {_verr}"}},
                status_code=400)
        # === PRE-SEND GATE: hard invariant ===
        _pre_input = estimate_full_payload_tokens(
            vllm_body["messages"], vllm_body.get("tools", []), _note_tok(), session_key
        )
        _pre_total = _pre_input + vllm_body["max_tokens"]
        if _pre_total > MAX_CONTEXT:
            log.warning(
                "Pre-send gate: input=%d + output=%d = %d > MAX_CONTEXT=%d - shrinking (session=%s)",
                _pre_input, vllm_body["max_tokens"], _pre_total, MAX_CONTEXT, session_key
            )
            _target_input = MAX_CONTEXT - vllm_body["max_tokens"] - SAFETY_MARGIN
            if _target_input < 500:
                _reduced_output = max(100, MAX_CONTEXT - _pre_input - SAFETY_MARGIN)
                vllm_body["max_tokens"] = _reduced_output
                _target_input = MAX_CONTEXT - _reduced_output - SAFETY_MARGIN
            if _target_input > 0:
                _recut_to(vllm_body["messages"], _target_input)
                _pre_input = estimate_full_payload_tokens(
                    vllm_body["messages"], vllm_body.get("tools", []), _note_tok(), session_key
                )
                vllm_body["max_tokens"] = _output_budget(_pre_input)
            log.info(
                "Pre-send gate resolved: input=%d + output=%d = %d <= MAX_CONTEXT=%d",
                _pre_input, vllm_body["max_tokens"],
                _pre_input + vllm_body["max_tokens"], MAX_CONTEXT
            )
        # === END PRE-SEND GATE ===
        while True:
            _attempts += 1
            _t0 = time.monotonic()
            resp = await client.post(VLLM_URL + "/chat/completions", json=vllm_body)
            _dt = time.monotonic() - _t0
            if _dt > 2.0:
                log.warning("vLLM pool-wait: %.1fs (possible pool saturation)", _dt)
            if resp.status_code in (500, 503) and _attempts < 3:
                _delay = 1.0 * _attempts
                log.warning("vLLM transient %d (attempt %d/3) - retrying in %.1fs", resp.status_code, _attempts, _delay)
                await asyncio.sleep(_delay)
                continue
            break
        if resp.status_code != 200:
            _et = resp.text.lower()
            _is_ctx_err = _is_context_length_error(resp.status_code, resp.text)
            if _is_ctx_err:
                # Phase 08: ONE forced whole-group recut, then fail
                log.warning("vLLM context-length error - ONE recut retry: %s", resp.text[:200])
                calibrator.bump_margin(session_key)
                
                # Parse actual token count from vLLM error for accurate recut target
                _actual_in, _actual_max = _parse_vllm_actual_tokens(resp.text)
                if _actual_in:
                    _desired_out = min(_actual_max or MIN_OUTPUT, MAX_OUTPUT)
                    target = MAX_CONTEXT - SAFETY_MARGIN - _desired_out
                    log.info("Recut using vLLM actual: input=%d max_tokens=%d target_input=%d",
                             _actual_in, _desired_out, target)
                    # Hard cut: use vLLM's exact count, no estimation
                    _hard_cut_to(vllm_body["messages"], _actual_in, target)
                else:
                    target = _trim_target()
                    _recut_to(vllm_body["messages"], target)
                
                new_input_tokens = estimate_full_payload_tokens(vllm_body["messages"], vllm_body.get("tools", []), _note_tok(), session_key)
                _budget = _output_budget(new_input_tokens)
                max_tokens = min(_budget, MAX_OUTPUT, CTXGATE_MAX_TOTAL_OUTPUT)
                vllm_body["max_tokens"] = max_tokens
                input_tokens = new_input_tokens
                # Phase 08: validate the recut payload before retrying
                _vok, _verr = _validate_outgoing_payload(vllm_body, live_prompt_hash)
                if not _vok:
                    metrics["requests_error"] += 1
                    log.error("Recut payload validation failed: %s (session=%s)", _verr, session_key)
                    _record_call(session_key, input_tokens, 0, f"recut_validation_failed:{_verr}", VLLM_MODEL, False, _verr)
                    return JSONResponse(
                        {"error": {"type": _verr,
                         "message": f"Payload validation failed after recut: {_verr}"}},
                        status_code=400)
                resp = await client.post(VLLM_URL + "/chat/completions", json=vllm_body)
                if resp.status_code != 200:
                    metrics["requests_error"] += 1
                    log.error("vLLM recut retry also failed %d: %s", resp.status_code, resp.text[:500])
                    _record_call(session_key, input_tokens, 0, f"vllm_{resp.status_code}", VLLM_MODEL, False, resp.text[:300])
                    if _is_context_length_error(resp.status_code, resp.text):
                        return JSONResponse(
                            {"error": {"type": "context_length_exceeded",
                             "message": "Context length exceeded even after one recut retry. Reduce prompt size.",
                             "input_tokens": input_tokens}},
                            status_code=400)
                    return JSONResponse({"error": {"message": "vLLM " + str(resp.status_code), "explanation": explain_status(f"vllm_{resp.status_code}", resp.text[:300])}}, status_code=resp.status_code)
            else:
                # Phase 08: non-context errors (401, 403, 429, 500, 503, non-context 400) pass through
                metrics["requests_error"] += 1
                log.error("vLLM error %d: %s", resp.status_code, resp.text[:500])
                _record_call(session_key, input_tokens, 0, f"vllm_{resp.status_code}", VLLM_MODEL, False, resp.text[:300])
                return JSONResponse({"error": {"message": "vLLM " + str(resp.status_code), "explanation": explain_status(f"vllm_{resp.status_code}", resp.text[:300])}}, status_code=resp.status_code)
        data = resp.json()
        if not isinstance(data.get("usage"), dict):
            data["usage"] = {}
        choices = data.get("choices", [])
        # --- Tool call sanitization ---
        for choice in choices:
            msg = choice.get("message", {})
            if msg.get("tool_calls"):
                cleaned, stripped = sanitize_tool_calls(msg)
                if stripped:
                    metrics["toolcall_strips"] += 1
                    metrics["tool_call_suppressed"] += 1
                    choice["message"]["tool_calls"] = None
                    choice["finish_reason"] = "stop"
        # --- Unbounded replace-loop guard ---
        if BLOCK_UNBOUNDED_LOOPS:
            for choice in choices:
                _tcs = choice.get("message", {}).get("tool_calls")
                if not _tcs:
                    continue
                for _tc in _tcs:
                    _fn = _tc.get("function", {})
                    if _fn.get("name") != "execute_typescript":
                        continue
                    try:
                        _args = json.loads(_fn.get("arguments", "{}"))
                    except Exception:
                        _args = {}
                    if _unbounded_replace_loop(_args.get("code", "")):
                        metrics["unsafe_loop_blocked"] += 1
                        log.warning("Blocked unbounded replace-loop in execute_typescript (session=%s)", session_key)
                        choice["message"]["tool_calls"] = None
                        choice["finish_reason"] = "stop"
                        _retry_msgs = list(vllm_body.get("messages", []))
                        _retry_msgs.append({"role": "system", "content": (
                            "RULE: Never write while(s.includes(A)) { s = s.replace(A, B) }. "
                            "If B contains A, it never terminates and freezes the sandbox. "
                            "For a global replacement use s.replaceAll(A, B) or s.split(A).join(B) \u2014 ONE pass, no loop. "
                            "Any loop in execute_typescript must have an explicit iteration cap (e.g. for (let i=0;i<1000;i++)). "
                            "After a bulk replace, count with s.split(A).length-1 once, do not loop on it."
                        )})
                        _retry_body = dict(vllm_body)
                        _retry_body["messages"] = _retry_msgs
                        _retry_body["max_tokens"] = max_tokens
                        try:
                            _r3 = await client.post(VLLM_URL + "/chat/completions", json=_retry_body)
                            if _r3.status_code == 200:
                                data = _r3.json()
                                if not isinstance(data.get("usage"), dict):
                                    data["usage"] = {}
                                choices = data.get("choices", [])
                                # Check if retry ALSO returned an unbounded loop
                                _retry_unsafe = False
                                for _rc in choices:
                                    _rtcs = _rc.get("message", {}).get("tool_calls")
                                    if not _rtcs:
                                        continue
                                    for _rtc in _rtcs:
                                        _rfn = _rtc.get("function", {})
                                        if _rfn.get("name") != "execute_typescript":
                                            continue
                                        try:
                                            _rargs = json.loads(_rfn.get("arguments", "{}"))
                                        except Exception:
                                            _rargs = {}
                                        if _unbounded_replace_loop(_rargs.get("code", "")):
                                            _retry_unsafe = True
                                            break
                                    if _retry_unsafe:
                                        break
                                if _retry_unsafe:
                                    metrics["unsafe_loop_blocked"] += 1
                                    log.warning("Unbounded-loop retry ALSO unsafe - returning text error (session=%s)", session_key)
                                    for _rc in choices:
                                        _rc["message"]["tool_calls"] = None
                                        _rc["message"]["content"] = "BLOCKED: execute_typescript code contains an unbounded while(var.includes(X)) { var = var.replace(X, Y) } loop. Use s.replaceAll(A, B) or s.split(A).join(B) instead. Any loop must have an explicit iteration cap (e.g. for (let i=0;i<1000;i++))."
                                        _rc["finish_reason"] = "stop"
                                else:
                                    log.info("Unbounded-loop retry succeeded (session=%s)", session_key)
                        except Exception as _e3:
                            log.warning("Unbounded-loop retry failed: %s", _e3)
                            for _rc in choices:
                                _rc["message"]["tool_calls"] = None
                                _rc["message"]["content"] = "BLOCKED: execute_typescript code contains an unbounded replace loop. Use s.replaceAll(A, B) or s.split(A).join(B) instead."
                                _rc["finish_reason"] = "stop"
                        break
                break
        output_tokens = data.get("usage", {}).get("completion_tokens", 0)
        total_output_tokens += output_tokens
        # --- Auto-continuation with total output budget ---
        ns_wall_start = time.time()
        while choices and choices[0].get("finish_reason") == "length" and cont_count < MAX_CONTINUATIONS:
            if time.time() - ns_wall_start > WALL_CLOCK_MAX:
                log.warning("Wall clock %ds exceeded - stopping non-stream", WALL_CLOCK_MAX)
                exit_reason = "wall_clock"
                break
            # Check total output budget
            remaining = CTXGATE_MAX_TOTAL_OUTPUT - total_output_tokens
            if remaining < CTXGATE_MIN_CONTINUATION_OUTPUT:
                log.info("Non-stream: total output budget exhausted (%d/%d) - stopping", total_output_tokens, CTXGATE_MAX_TOTAL_OUTPUT)
                exit_reason = "total_output_budget"
                metrics["output_total_budget_exhausted"] += 1
                break
            cont_count += 1
            metrics["output_continuations_total"] += 1
            log.info("Non-stream: auto-continuing (%d/%d, remaining=%d)", cont_count, MAX_CONTINUATIONS, remaining)
            msg_c = choices[0].get("message", {})
            # --- Continuation state machine ---
            has_tool_calls = bool(msg_c.get("tool_calls"))
            if has_tool_calls:
                # Complete tool-call set -> terminate, no continuation
                exit_reason = "tool_calls_complete"
                break
            trunc_type = _classify_truncation("length", msg_c.get("content",""), msg_c.get("reasoning_content",""), msg_c.get("tool_calls"))
            if trunc_type == "reasoning_overflow":
                cb = dict(vllm_body)
                cb["chat_template_kwargs"] = {"enable_thinking": False}
                _cb_budget = _output_budget(count_messages_tokens(vllm_body.get("messages", [])))
                cb["max_tokens"] = min(_cb_budget, MAX_OUTPUT, remaining)
                r2 = await client.post(VLLM_URL + "/chat/completions", json=cb)
                if r2.status_code == 200:
                    d2 = r2.json()
                    nc = d2.get("choices", [])
                    if nc and nc[0].get("message",{}).get("content"):
                        choices[0]["message"]["content"] = (msg_c.get("content","") or "") + nc[0]["message"]["content"]
                        choices[0]["finish_reason"] = nc[0].get("finish_reason", "stop")
                        _r2_tokens = d2.get("usage",{}).get("completion_tokens",0)
                        output_tokens += _r2_tokens
                        total_output_tokens += _r2_tokens
                exit_reason = "reasoning_overflow"
                break
            # Pure text continuation
            partial = choices[0].get("message", {}).get("content") or ""
            cont_msgs = list(vllm_body.get("messages", []))
            cont_msgs.append({"role": "assistant", "content": partial})
            cont_msgs.append({"role": "user", "content": "Continue from exactly where you left off. Do not repeat any content already provided. Resume the next word/sentence/code line."})
            cont_tokens = count_messages_tokens(cont_msgs)
            _cont_budget = _output_budget(cont_tokens)
            max_tokens = min(_cont_budget, MAX_OUTPUT, remaining)
            if cont_tokens > MAX_INPUT or max_tokens < CTXGATE_MIN_CONTINUATION_OUTPUT:
                log.info("Non-stream cont: would exceed input budget or starve output - stopping")
                exit_reason = "continuation_budget"
                break
            cont_body = dict(vllm_body)
            cont_body["messages"] = cont_msgs
            cont_body["max_tokens"] = max_tokens
            resp = await client.post(VLLM_URL + "/chat/completions", json=cont_body)
            if resp.status_code != 200:
                exit_reason = "continuation_error"
                break
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                new_c = choices[0].get("message", {}).get("content", "")
                if new_c:
                    choices[0]["message"]["content"] = partial + new_c
                _cont_tokens = data.get("usage", {}).get("completion_tokens", 0)
                output_tokens += _cont_tokens
                total_output_tokens += _cont_tokens
        # --- Finalize: add ctxgate meta ---
        truncated = exit_reason != "ok"
        tool_calls_complete = False
        tool_calls_emitted = 0
        for choice in choices:
            if choice.get("finish_reason") == "length" and choice.get("message", {}).get("content"):
                choice["message"]["content"] = _safe_truncate(choice["message"]["content"])
                choice["finish_reason"] = "length"
            if choice.get("message", {}).get("tool_calls"):
                tool_calls_complete = True
                tool_calls_emitted = len(choice["message"]["tool_calls"])
        if tool_calls_complete:
            metrics["tool_call_complete"] += 1
        if truncated:
            metrics["output_truncated_total"] += 1
            _reason_key = exit_reason if exit_reason in metrics.get("output_truncated_by_reason", {}) else exit_reason
            if _reason_key not in metrics["output_truncated_by_reason"]:
                metrics["output_truncated_by_reason"][_reason_key] = 0
            metrics["output_truncated_by_reason"][_reason_key] += 1
        data["ctxgate"] = ctxgate_meta(
            truncated=truncated,
            reason=exit_reason,
            continuations_used=cont_count,
            total_output_tokens=total_output_tokens,
            tool_calls_complete=tool_calls_complete,
            tool_calls_emitted=tool_calls_emitted,
        )["ctxgate"]
        metrics["tokens_out_total"] += output_tokens
        metrics["requests_ok"] += 1
        _ns_cached = data.get("usage", {}).get("prompt_tokens_details", {}).get("cached_tokens", 0)
        _real_pt = data["usage"].get("prompt_tokens", 0)
        _final_pt = _real_pt if _real_pt > 0 else input_tokens
        if _real_pt > 0:
            calibrator.observe(session_key, input_tokens, _real_pt)
            log.info("TOKEST est=%d real=%d err=%+.1f%% ratio=%.3f m=%.3f session=%s",
                     input_tokens, _real_pt, 100.0*(input_tokens-_real_pt)/_real_pt,
                     calibrator.ratio, calibrator.get_rho(session_key)[1], session_key)
        metrics["cached_tokens_total"] += _ns_cached
        metrics["prompt_tokens_total"] += _final_pt
        from proxy.hotpath.tracking import store_exact_tokens
        store_exact_tokens(session_key, _final_pt)
        _track_session_tokens(session_key, 0, output_tokens, count_req=False)
        _record_call(session_key, _final_pt, output_tokens, "ok", VLLM_MODEL, False, cached_tokens=_ns_cached)
        data["usage"]["completion_tokens"] = output_tokens
        data["usage"]["prompt_tokens"] = _final_pt
        # Invariant: cached_tokens must never exceed prompt_tokens
        _ptd = data["usage"].get("prompt_tokens_details")
        if _ptd and _ptd.get("cached_tokens", 0) > data["usage"]["prompt_tokens"]:
            _ptd["cached_tokens"] = data["usage"]["prompt_tokens"]
        data["usage"]["total_tokens"] = data["usage"]["prompt_tokens"] + output_tokens
        for ch in data.get("choices", []):
            msg = ch.get("message", {})
            if "reasoning" in msg and "reasoning_content" not in msg:
                msg["reasoning_content"] = msg.pop("reasoning")
        if exit_reason not in ("ok", "tool_calls_complete"):
            log.warning("NS-DIAG session=%s exit=%s finish=%s truncated=%s conts=%d total_out=%d tc_seen=%d tc_complete=%d tc_emitted=%d",
                        session_key, exit_reason, choices[0].get("finish_reason","?") if choices else "?",
                        truncated, cont_count, total_output_tokens, tool_calls_emitted, tool_calls_complete, tool_calls_emitted)
        return JSONResponse(data)
    except httpx.TimeoutException:
        metrics["requests_error"] += 1
        _record_call(session_key, input_tokens, 0, "timeout", VLLM_MODEL, False, "vLLM 300s timeout")
        return JSONResponse({"error": {"message": "vLLM timeout", "explanation": explain_status("timeout")}, "ctxgate": ctxgate_meta(True, "timeout", 0, 0, False, 0)["ctxgate"]}, status_code=504)
    except ContextCapacityError as e:
        metrics["requests_error"] += 1
        log.error("Context capacity error: %s", e)
        return JSONResponse({"error": {"message": str(e), "explanation": explain_status("context_capacity")}, "ctxgate": ctxgate_meta(True, "context_capacity", 0, 0, False, 0)["ctxgate"]}, status_code=413)
    except Exception as e:
        metrics["requests_error"] += 1
        log.exception("vLLM forward error: %s", e)
        _record_call(session_key, input_tokens, 0, "error", VLLM_MODEL, False, str(e)[:300])
        return JSONResponse({"error": {"message": str(e), "explanation": explain_status("error", str(e)[:300])}, "ctxgate": ctxgate_meta(True, "error", 0, 0, False, 0)["ctxgate"]}, status_code=500)

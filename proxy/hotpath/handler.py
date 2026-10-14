"""Main chat completions handler: the hot path entry point."""
import asyncio
import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from proxy.config import (
    CTXGATE_TENANT_RATE_PER_SEC, CTXGATE_TENANT_MAX_CONCURRENCY,
    CTXGATE_PROACTIVE_TRIM_FRAC,
    MAX_BODY_BYTES, MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN, MAX_OUTPUT,
    MAX_REASONING_TOKENS,
    TRIM_TARGET_TOKENS, TRIM_TARGET_FRACTION, TRIM_TARGET_FLOOR,
    VLLM_MODEL,
)
# _rho_var removed: using exact gigatoken counting
from proxy.token.exact import exact_count_messages
from proxy.window.budget import _output_budget
from proxy.window.invariants import _live_prompt_identity
from proxy.window.shrink import ContextCapacityError, _emergency_shrink
from proxy.window.cut import _trim_target, _hard_cut_to
from proxy.streaming import stream_to_vllm, stream_to_vllm_passthrough
from proxy.streaming import forward_to_vllm, forward_to_vllm_passthrough
from proxy.resilience.rate_limit import _get_tenant_rate_limiter, _get_tenant_semaphore
from proxy.hotpath.context import build_context
from proxy.hotpath.messaging import _repair_dangling_tool_calls
from proxy.hotpath.injection import _apply_epoch_injection
from proxy.hotpath.session_id import _resolve_session_identity
from proxy.hotpath.vllm_body import _build_vllm_body
from proxy.hotpath.tracking import _track_session_tokens
from proxy.utils.helpers import _with_compaction_note, ctxgate_meta, _note_tok
from proxy.lifecycle.recovery import _spawn

log = logging.getLogger("ctxgate-proxy")

_inflight_count = 0
_shutting_down = False

# In-memory 413 counter per session (reset on successful response)
_413_counts: dict[str, int] = {}


def _is_empty_completion(result) -> bool:
    """Check if a non-streaming vLLM response has empty/whitespace-only content."""
    import json as _json
    if not isinstance(result, JSONResponse):
        return False
    try:
        data = _json.loads(result.body)
    except Exception:
        return True
    choices = data.get("choices", [])
    if not choices:
        return True
    msg = choices[0].get("message", {})
    content = msg.get("content")
    if content is None:
        return True
    if isinstance(content, str) and not content.strip():
        return True
    return False


def _minimal_valid_response(reason: str, model: str = "local-model", content: str = "") -> JSONResponse:
    """Return a valid HTTP 200 OpenAI chat completion.

    ONLY for truly unfixable cases: vLLM unreachable after all retries.
    All other issues are solved internally by the proxy before responding.
    """
    import time as _time
    import uuid as _uuid
    if not content:
        content = "Request could not be processed. Please resend."
    log.error("MINIMAL 200 (last resort): reason=%s model=%s", reason, model)
    return JSONResponse({
        "id": f"chatcmpl-{_uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(_time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 10, "total_tokens": 10}
    })


def _invalidate_baseline(session_key: str):
    """Deprecated no-op: legacy token-ratio tracking removed after gigatoken migration."""


def _full_context_reset(messages: list) -> list:
    """Reduce to minimal viable context: system + first user + last user."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return messages[:1] if messages else messages
    result = list(system_msgs)
    result.append(user_msgs[0])
    if len(user_msgs) > 1:
        result.append(user_msgs[-1])
    return result


def _aggressive_trim(built: list, target_tokens: int, session_key: str,
                    total_input_tokens: int | None = None) -> list:
    """Aggressively trim a built context to fit within target_tokens."""
    inject_overhead = 0
    _msg_tok: int | None = None
    if total_input_tokens is not None:
        _msg_tok = exact_count_messages(built, VLLM_MODEL)
        inject_overhead = max(0, total_input_tokens - _msg_tok)
    effective_target = max(1, target_tokens - inject_overhead)

    current_tokens = _msg_tok if _msg_tok is not None else exact_count_messages(built, VLLM_MODEL)
    if current_tokens <= effective_target:
        _invalidate_baseline(session_key)
        return built

    log.info("AGGRESSIVE TRIM: session=%s current=%d eff_target=%d (target=%d inject_ovhd=%d) msgs=%d",
             session_key, current_tokens, effective_target, target_tokens, inject_overhead, len(built))

    # Step 1: aggressive emergency shrink (unprotects tool groups)
    try:
        trimmed = _emergency_shrink(built, effective_target, aggressive=True)
        trimmed_tokens = exact_count_messages(trimmed, VLLM_MODEL)
        if trimmed_tokens <= effective_target:
            log.info("AGGRESSIVE TRIM: success (aggressive) %d -> %d tokens (eff_target=%d)",
                     current_tokens, trimmed_tokens, effective_target)
            _invalidate_baseline(session_key)
            return trimmed
    except ContextCapacityError:
        pass

    # Step 2: with injection pruning
    try:
        trimmed = _emergency_shrink(built, effective_target, aggressive=True, allow_injection_prune=True)
        trimmed_tokens = exact_count_messages(trimmed, VLLM_MODEL)
        if trimmed_tokens <= effective_target:
            log.info("AGGRESSIVE TRIM: success (injection_prune) %d -> %d tokens (eff_target=%d)",
                     current_tokens, trimmed_tokens, effective_target)
            _invalidate_baseline(session_key)
            return trimmed
    except ContextCapacityError:
        pass

    # Step 3: nuclear - minimal viable context
    log.warning("AGGRESSIVE TRIM: nuclear (minimal viable) session=%s", session_key)
    system_msgs = [m for m in built if m.get("role") == "system"]
    user_msgs = [m for m in built if m.get("role") == "user"]
    trimmed = list(system_msgs)
    if user_msgs:
        trimmed.append(user_msgs[0])
        if len(user_msgs) > 1:
            trimmed.append(user_msgs[-1])
    else:
        trimmed = built[:1] if built else []
    log.info("AGGRESSIVE TRIM: nuclear result %d -> %d tokens (msgs=%d)", current_tokens, trimmed_tokens, len(trimmed))
    _invalidate_baseline(session_key)
    return trimmed


async def _forward_to_vllm(vllm_body: dict, input_tokens: int, session_key: str,
                           _client_cap, _live_prompt_hash: str, stream: bool = False):
    """Unified vLLM forwarding: picks the right function based on client_cap and stream."""
    if _client_cap is not None:
        if stream:
            return await stream_to_vllm_passthrough(vllm_body, input_tokens, session_key)
        else:
            return await forward_to_vllm_passthrough(vllm_body, input_tokens, session_key)
    else:
        if stream:
            return await stream_to_vllm(vllm_body, input_tokens, session_key, _live_prompt_hash)
        else:
            return await forward_to_vllm(vllm_body, input_tokens, session_key, _live_prompt_hash)


async def chat_completions(request: Request):
    import proxy.app as _app
    global _inflight_count, _shutting_down
    _inflight_count += 1
    _sem_acquired = False
    _sem = None
    try:
        # ── Shutdown Drain ──
        if _shutting_down:
            log.info("Shutdown drain: processing request")

        # ── Rate Limit & Concurrency ──
        tenant_id = getattr(request.state, "tenant_id", "noauth")

        if CTXGATE_TENANT_RATE_PER_SEC > 0:
            _tl = _get_tenant_rate_limiter(tenant_id)
            _tl._refill()
            if _tl.tokens < 1:
                _deficit = 1 - _tl.tokens
                _wait = min(5.0, _deficit / max(_tl.rate, 0.1) + 0.1)
                log.info("Rate limit: tenant=%s waiting %.1fs", tenant_id, _wait)
                await asyncio.sleep(_wait)
                _tl._refill()
            _tl.tokens -= 1

        if CTXGATE_TENANT_MAX_CONCURRENCY > 0:
            _sem = _get_tenant_semaphore(tenant_id)
            try:
                await asyncio.wait_for(_sem.acquire(), timeout=30)
                _sem_acquired = True
            except asyncio.TimeoutError:
                log.warning("CONCURRENCY LIMIT REACHED: tenant=%s sem_full after 30s", tenant_id)
                _app.metrics["concurrency_limit_reached"] = _app.metrics.get("concurrency_limit_reached", 0) + 1
                return JSONResponse(
                    status_code=503,
                    headers={"Retry-After": "5"},
                    content={
                        "error": {
                            "message": "Server busy, please retry shortly",
                            "type": "overloaded",
                            "retry_after": 5,
                        }
                    },
                )

        # ── Body Parsing & Model Routing ──
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            log.warning("Body large (%s > %d) - trimming in build_context", cl, MAX_BODY_BYTES)

        _app.metrics["requests_total"] += 1
        try:
            body = await request.json()
        except Exception as _je:
            _app.metrics["requests_error"] += 1
            log.error("Invalid JSON (unparseable): %s", str(_je)[:200])
            return _minimal_valid_response("invalid_json", content="Request body is not valid JSON. Please resend.")

        model = body.get("model", "")
        if model and model != VLLM_MODEL:
            log.info("Model %r -> %r", model, VLLM_MODEL)
            body["model"] = VLLM_MODEL
            model = VLLM_MODEL

        messages = body.get("messages", [])
        if not messages:
            _app.metrics["requests_error"] += 1
            log.error("No messages in request")
            return _minimal_valid_response("no_messages", content="No messages in request. Please resend.")

        # ── Live Prompt Identity ──
        _live_prompt_hash = _live_prompt_identity(messages)

        # ── Session Identity ──
        sess = await _resolve_session_identity(request, messages, body)
        session_key = sess["session_key"]
        from proxy.app import _session_ctx
        _session_ctx["id"] = session_key
        # Exact counting mode: no rho calibration needed
        task_uuid = sess["uuid"]
        x_sid = sess["id"]
        _client_cap = sess["client_cap"]
        log.info("%s sid=%s session_key=%s task=%s cap=%s",
                 sess["log_label"], sess["id"], session_key, task_uuid, _client_cap)

        # ── Background Task Enqueue ──
        last_user_content = ''
        for m in reversed(messages):
            if m.get('role') == 'user':
                uc = m.get('content', '')
                if isinstance(uc, list):
                    last_user_content = ' '.join(
                        p.get('text', '') for p in uc if isinstance(p, dict))
                else:
                    last_user_content = uc or ''
                break
        if last_user_content:
            _spawn(_bg_ensure_task_and_enqueue(sess, tenant_id, x_sid, last_user_content))

        # ── Build Context (the cut) ──
        _t0 = time.monotonic()
        _goose_toks = body.get("usage", {}).get("total_tokens")
        try:
            built, _toks = await build_context(messages, task_uuid=task_uuid, session_key=session_key, goose_reported_tokens=_goose_toks)
        except ContextCapacityError as cce:
            log.warning("Context capacity: %s - full reset", cce)
            _reset_msgs = _full_context_reset(messages)
            try:
                built, _toks = await build_context(_reset_msgs, task_uuid=task_uuid, session_key=session_key, goose_reported_tokens=_goose_toks)
                log.info("Capacity recovered via full reset: session=%s", session_key)
                _invalidate_baseline(session_key)
            except ContextCapacityError as cce2:
                log.error("Capacity unrecoverable: %s", cce2)
                return _minimal_valid_response("context_capacity",
                    content="Context too large even after reset. Please start a new session.")
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: build_context %.0fms (msgs=%d)", _dt, len(messages))

        # ── Epoch-Freeze Injection ──
        built = await _apply_epoch_injection(
            built, messages, session_key, task_uuid, x_sid, tenant_id)
        _live_prompt_hash = _live_prompt_identity(built)

        # ── Repair & Annotate ──
        built = _repair_dangling_tool_calls(built)
        built = _with_compaction_note(built)

        # ── Token Count (single source of truth) ──
        _note_n = _note_tok()
        from proxy.hotpath.tracking import get_exact_tokens, recover_session_baseline
        if get_exact_tokens(session_key) is None:
            _recovered = recover_session_baseline(session_key)
            if _recovered is not None:
                from proxy.hotpath.tracking import store_exact_tokens
                store_exact_tokens(session_key, _recovered)
                log.info("Recovered session baseline from Goose DB: session=%s input_tokens=%d", session_key, _recovered)
        _t0 = time.monotonic()
        input_tokens = exact_count_messages(built, VLLM_MODEL)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: exact_count_messages %.0fms (tokens=%d)", _dt, input_tokens)

        # ── Metrics Tracking ──
        _app.metrics["max_context_seen"] = max(_app.metrics.get("max_context_seen", 0), input_tokens)
        _app.metrics["tokens_in_total"] += input_tokens
        _track_session_tokens(session_key, input_tokens)

        # ── Cross-Session Extraction ──
        if _app._EXTRACT_IN_FLIGHT < 2:
            _spawn(_fire_and_forget_extract(x_sid, session_key, messages, tenant_id=tenant_id))
        else:
            _app.metrics["extract_shed"] = _app.metrics.get("extract_shed", 0) + 1

        # ── Thinking Flag (stable per turn) ──
        _ws = _app.session_compactions.get(session_key)
        _disable_thinking = False
        if _ws is not None:
            _stored_thinking = _ws.get("thinking_disabled")
            if _stored_thinking is not None:
                _disable_thinking = _stored_thinking
            _current_live_hash = _live_prompt_hash
            _stored_live_hash = _ws.get("live_prompt_hash")
            if _stored_live_hash != _current_live_hash:
                _disable_thinking = False
                _ws["live_prompt_hash"] = _current_live_hash

        _think_trigger = MAX_INPUT
        if not _disable_thinking and input_tokens > _think_trigger:
            _disable_thinking = True
            log.info("THINKING DISABLED: session=%s input=%d > trigger=%d",
                     session_key, input_tokens, _think_trigger)

        # ── Output Budget ──
        if _disable_thinking:
            _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
        else:
            _budget = _output_budget(input_tokens)

        # ── Hard Ceiling Enforcement ──
        _safe_max_input = MAX_INPUT
        if input_tokens > _safe_max_input:
            log.warning("HARD CEILING: session=%s input=%d exceeds safe_max=%d",
                        session_key, input_tokens, _safe_max_input)
            _app.metrics["hard_ceiling_trim"] = _app.metrics.get("hard_ceiling_trim", 0) + 1

        # ── Proactive Trim ──
        if _budget < 1:
            log.warning("PROACTIVE TRIM: session=%s input=%d leaves no output room (budget=%d)",
                        session_key, input_tokens, _budget)
            max_allowed_input = MAX_CONTEXT - MAX_OUTPUT - SAFETY_MARGIN
            _app.metrics["proactive_trim"] = _app.metrics.get("proactive_trim", 0) + 1

            trimmed = _aggressive_trim(built, max_allowed_input, session_key, input_tokens)
            built = trimmed
            input_tokens = exact_count_messages(built, VLLM_MODEL)

            if _disable_thinking:
                _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
            else:
                _budget = _output_budget(input_tokens)

            if _budget < 1:
                log.warning("PROACTIVE TRIM: nuclear reset session=%s input=%d", session_key, input_tokens)
                _minimal = _full_context_reset(built)
                built = _minimal
                _invalidate_baseline(session_key)
                input_tokens = exact_count_messages(built, VLLM_MODEL)
                if _disable_thinking:
                    _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
                else:
                    _budget = _output_budget(input_tokens)

            if _budget < 1:
                log.warning("PROACTIVE TRIM: content truncation session=%s input=%d",
                            session_key, input_tokens)
                _truncated_any = False
                for _m in built:
                    _c = _m.get("content", "")
                    if isinstance(_c, str) and len(_c) > 100:
                        _ratio = max(0.05, (max_allowed_input * 0.9) / max(input_tokens, 1))
                        _m["content"] = _c[:int(len(_c) * _ratio)]
                        _truncated_any = True
                if _truncated_any:
                    _invalidate_baseline(session_key)
                    _live_prompt_hash = _live_prompt_identity(built)
                    input_tokens = exact_count_messages(built, VLLM_MODEL)
                    if _disable_thinking:
                        _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
                    else:
                        _budget = _output_budget(input_tokens)

            if _budget < 1:
                _budget = 1
                _disable_thinking = True
                if _ws is not None:
                    _ws["thinking_disabled"] = True

            log.info("PROACTIVE TRIM: resolved session=%s input=%d budget=%d", session_key, input_tokens, _budget)

        max_tokens = min(_budget, _client_cap) if _client_cap is not None else _budget
        if max_tokens < 1:
            max_tokens = 1
        if max_tokens < MAX_OUTPUT and _client_cap is None:
            if input_tokens + MAX_OUTPUT + SAFETY_MARGIN <= MAX_CONTEXT:
                max_tokens = MAX_OUTPUT

        if _disable_thinking and _ws is not None:
            _ws["thinking_disabled"] = True

        # ── Window State Log ──
        _raw_tokens = getattr(_app, "_pre_injection_tokens", input_tokens)
        _injected_delta = input_tokens - _raw_tokens
        _ctx_path = getattr(_app, "last_context_path", "unknown")
        if _ctx_path == "none" and input_tokens > MAX_INPUT:
            _ctx_path = "overflow"
        _msg_counts = getattr(_app, "last_msg_counts", {})
        _prefix = getattr(_app, "last_prefix_cache", "n/a")
        _total_msgs = _msg_counts.get("total", len(messages))
        _stable = _msg_counts.get("stable", 0)
        _kept = _msg_counts.get("kept", len(built))
        _new = _msg_counts.get("new", 0)
        _think_str = "OFF" if _disable_thinking else "ON"
        # ── Forward to vLLM ──
        stream = body.get("stream", False)
        vllm_body = _build_vllm_body(built, body, max_tokens, stream, _client_cap, session_key, disable_thinking=_disable_thinking)
        result = await _forward_to_vllm(vllm_body, input_tokens, session_key, _client_cap, _live_prompt_hash, stream)

        # ── Window stats log (with real output tokens) ──
        def _emit_window_log(_real_out):
            log.warning(
                "WINDOW: total=%d kept=%d(%ds+%dn) drop=%d | cut=%s | prefix=%s | raw=%d in=%d injected=%d out=%s think=%s",
                _total_msgs, _kept, _stable, _new, _msg_counts.get("dropped", _total_msgs - _kept), _ctx_path, _prefix,
                _raw_tokens, input_tokens, _injected_delta, _real_out, _think_str,
            )

        if stream:
            async def _logged_stream(gen, _log_fn=_emit_window_log):
                last_chunk = None
                async for chunk in gen:
                    last_chunk = chunk
                    yield chunk
                _real_out = "?"
                try:
                    if last_chunk:
                        _txt = last_chunk if isinstance(last_chunk, str) else last_chunk.decode("utf-8", errors="replace")
                        for line in reversed(_txt.split("\n")):
                            line = line.strip()
                            if line.startswith("data:") and line != "data: [DONE]":
                                import json as _json_ws
                                _payload = _json_ws.loads(line[5:].strip())
                                _usage = _payload.get("usage")
                                if _usage and "completion_tokens" in _usage:
                                    _real_out = _usage["completion_tokens"]
                                    break
                except Exception:
                    pass
                _log_fn(_real_out)
            result = StreamingResponse(_logged_stream(result.body_iterator), media_type=result.media_type)
        else:
            _real_out = max_tokens
            if result is not None and isinstance(result, JSONResponse):
                try:
                    import json as _json_w
                    _w_data = _json_w.loads(result.body)
                    _real_out = _w_data.get("usage", {}).get("completion_tokens", max_tokens)
                except Exception:
                    pass
            _emit_window_log(_real_out)

        # ── Empty Response Retry ──
        if result is not None and not stream and _is_empty_completion(result):
            log.warning("EMPTY RESPONSE: session=%s input=%d max=%d - trimming and retrying",
                        session_key, input_tokens, max_tokens)
            _app.metrics["empty_response_retries"] = _app.metrics.get("empty_response_retries", 0) + 1

            max_allowed_input = MAX_CONTEXT - MAX_OUTPUT - SAFETY_MARGIN
            trimmed = _aggressive_trim(built, max_allowed_input, session_key, input_tokens)
            if trimmed is not built:
                built = trimmed
                input_tokens = exact_count_messages(built, VLLM_MODEL)
                max_tokens = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
                if max_tokens < 1:
                    max_tokens = 1
                _disable_thinking = True
                if _ws is not None:
                    _ws["thinking_disabled"] = True

                vllm_body = _build_vllm_body(built, body, max_tokens, False, _client_cap, session_key, disable_thinking=True)
                log.info("EMPTY RESPONSE RETRY: session=%s input=%d max=%d msgs=%d",
                         session_key, input_tokens, max_tokens, len(built))
                result = await _forward_to_vllm(vllm_body, input_tokens, session_key, _client_cap, _live_prompt_hash)

        # ── Finish Reason Length Handling ──
        if result is not None and not stream and isinstance(result, JSONResponse):
            try:
                import json as _json_fr
                _fr_data = _json_fr.loads(result.body)
                _fr_choices = _fr_data.get('choices', [])
                if _fr_choices:
                    _fr = _fr_choices[0].get('finish_reason')
                    if _fr == 'length':
                        log.warning('FINISH_RETRY_START: session=%s', session_key)
                        _app.metrics['finish_length_retry'] = _app.metrics.get('finish_length_retry', 0) + 1

                        _cont_msgs = built
                        _max_retries = 6
                        _success = False

                        for _attempt in range(_max_retries):
                            _resp_data = _json_fr.loads(result.body)
                            _resp_usage = _resp_data.get('usage', {})
                            _ew_total = _resp_usage.get('total_tokens', 0)
                            _resp_fr = _resp_data.get('choices', [{}])[0].get('finish_reason', '')
                            _limit = MAX_CONTEXT - SAFETY_MARGIN - max_tokens

                            if _ew_total <= _limit and _resp_fr != 'length':
                                log.info('FINISH_RETRY_SUCCESS: session=%s attempt=%d ew_total=%d limit=%d',
                                         session_key, _attempt+1, _ew_total, _limit)
                                _app.metrics['finish_length_success'] = _app.metrics.get('finish_length_success', 0) + 1
                                _success = True
                                break
                            elif _ew_total <= _limit and _resp_fr == 'length':
                                log.info('FINISH_RETRY_RESUBMIT: session=%s attempt=%d ew_total=%d',
                                         session_key, _attempt+1, _ew_total)
                                _retry_result = await _forward_to_vllm(vllm_body, input_tokens, session_key, _client_cap, _live_prompt_hash)
                                if _retry_result is None:
                                    continue
                                result = _retry_result
                            else:
                                log.warning('FINISH_RETRY_SHRINK: session=%s attempt=%d ew_total=%d > limit=%d fr=%s',
                                           session_key, _attempt+1, _ew_total, _limit, _resp_fr)
                                _ceiling = MAX_INPUT - 1024
                                _shrunken = _emergency_shrink(_cont_msgs, _ceiling, aggressive=True)
                                if len(_shrunken) >= len(_cont_msgs):
                                    break
                                _cont_msgs = _shrunken
                                input_tokens = exact_count_messages(_cont_msgs, VLLM_MODEL)
                                max_tokens = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
                                if max_tokens < 1:
                                    max_tokens = 1
                                vllm_body = _build_vllm_body(_cont_msgs, body, max_tokens, False, _client_cap, session_key, disable_thinking=_disable_thinking)
                                _retry_result = await _forward_to_vllm(vllm_body, input_tokens, session_key, _client_cap, _live_prompt_hash)
                                if _retry_result is None:
                                    continue
                                result = _retry_result

                        if not _success:
                            log.warning('FINISH_RETRY_EXHAUSTED: session=%s delivering partial after %d attempts', session_key, _max_retries)
                            _app.metrics['finish_length_fallback'] = _app.metrics.get('finish_length_fallback', 0) + 1
                            try:
                                _fallback_data = _json_fr.loads(result.body)
                                _fb_choices = _fallback_data.get('choices', [])
                                if _fb_choices:
                                    _fb_msg = _fb_choices[0].get('message', {})
                                    _fb_content = _fb_msg.get('content', '') or ''
                                    _fb_msg['content'] = _fb_content + '\n\n[TRUNCATED: response incomplete after max retry attempts.]'
                                    _fb_choices[0]['message'] = _fb_msg
                                    _fallback_data['choices'][0] = _fb_choices[0]
                                    _new_fb = _json_fr.dumps(_fallback_data).encode()
                                    result.body = _new_fb
                                    result.headers['content-length'] = str(len(_new_fb))
                            except Exception:
                                pass
            except Exception:
                pass

        # ── Final Fallback ──
        if result is not None and _is_empty_completion(result):
            log.warning("EMPTY RESPONSE PERSISTS: session=%s - trying full reset", session_key)
            _minimal = _full_context_reset(built)
            _reset_tokens = exact_count_messages(_minimal, VLLM_MODEL)
            _vllm_body = _build_vllm_body(_minimal, body, MAX_OUTPUT, False, _client_cap, session_key, disable_thinking=True)
            _rr = await _forward_to_vllm(_vllm_body, _reset_tokens, session_key, _client_cap, _live_prompt_hash)
            if _rr is not None and not _is_empty_completion(_rr):
                result = _rr

        if result is not None and _is_empty_completion(result):
            log.error("EMPTY RESPONSE UNRECOVERABLE: session=%s", session_key)
            _app.metrics["empty_response_unrecoverable"] = _app.metrics.get("empty_response_unrecoverable", 0) + 1
            result = _minimal_valid_response("empty_response", model=VLLM_MODEL,
                content="I'm having difficulty processing that request. Could you please rephrase it?")

        # ── Success ──
        if result is not None:
            _413_counts.pop(session_key, None)
        return result
    finally:
        _inflight_count -= 1
        if _sem_acquired:
            _sem.release()


def _bg_ensure_task_and_enqueue(goose_meta: dict, tenant_id: str, x_sid: str, last_user_content: str):
    """Background: ensure task row exists and enqueue memory extraction."""
    import proxy.app as _app
    from proxy.memory.job import _bg_ensure_task_and_enqueue as _job_fn
    return _job_fn(goose_meta, tenant_id, x_sid, last_user_content)


def _fire_and_forget_extract(x_sid: str, session_key: str, messages: list, tenant_id: str = ""):
    """Fire-and-forget cross-session knowledge extraction."""
    import proxy.app as _app
    from proxy.memory.extract import _fire_and_forget_extract as _fe
    _app._EXTRACT_IN_FLIGHT += 1
    async def _wrapper():
        try:
            await _fe(x_sid, session_key, messages, tenant_id=tenant_id)
        finally:
            _app._EXTRACT_IN_FLIGHT -= 1
    return _wrapper()

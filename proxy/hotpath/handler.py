"""Main chat completions handler: the hot path entry point."""
import asyncio
import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from proxy.config import (
    CTXGATE_TENANT_RATE_PER_SEC, CTXGATE_TENANT_MAX_CONCURRENCY,
    CTXGATE_INPUT_ESTIMATE_HEADROOM,
    MAX_BODY_BYTES, MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN, MIN_OUTPUT,
    VLLM_MODEL,
)
from proxy.token.calibrator import calibrator
from proxy.token.counter import _rho_var
from proxy.window.budget import estimate_full_payload_tokens, _output_budget
from proxy.window.invariants import _live_prompt_identity
from proxy.window.shrink import ContextCapacityError, _emergency_shrink
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


async def chat_completions(request: Request):
    import proxy.app as _app
    global _inflight_count, _shutting_down
    _inflight_count += 1
    _sem_acquired = False
    _sem = None
    try:
        if _shutting_down:
            return JSONResponse({"error": "shutting down"}, status_code=503)

        # Phase 2: tenant_id from auth dependency
        tenant_id = getattr(request.state, "tenant_id", "noauth")

        # Phase 2: per-tenant rate limiting (skipped when rate is 0)
        if CTXGATE_TENANT_RATE_PER_SEC > 0:
            _tl = _get_tenant_rate_limiter(tenant_id)
            _tl._refill()
            if _tl.tokens < 1:
                _deficit = 1 - _tl.tokens
                _retry_after = max(1, int(_deficit / _tl.rate) + 1)
                _app.metrics["requests_error"] += 1
                return JSONResponse(
                    {"error": {"message": "Rate limit exceeded"}},
                    status_code=429,
                    headers={"Retry-After": str(_retry_after)},
                )
            _tl.tokens -= 1

        # Phase 2: per-tenant concurrency semaphore (skipped when 0)
        if CTXGATE_TENANT_MAX_CONCURRENCY > 0:
            _sem = _get_tenant_semaphore(tenant_id)
            _sem_acquired = False
            try:
                await asyncio.wait_for(_sem.acquire(), timeout=30)
                _sem_acquired = True
            except asyncio.TimeoutError:
                _app.metrics["requests_error"] += 1
                return JSONResponse(
                    {"error": {"message": "Concurrency limit reached, try again shortly"}},
                    status_code=429,
                    headers={"Retry-After": "5"},
                )

        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            _app.metrics["requests_error"] += 1
            return JSONResponse({"error": {"message": "Request body too large"}}, status_code=413)

        _app.metrics["requests_total"] += 1
        try:
            body = await request.json()
        except Exception:
            _app.metrics["requests_error"] += 1
            return JSONResponse({"error": {"message": "Invalid JSON body"}}, status_code=400)

        model = body.get("model", "")
        if model and model != VLLM_MODEL:
            _app.metrics["requests_error"] += 1
            return JSONResponse({"error": {"message": "Unknown model: " + model}}, status_code=404)

        messages = body.get("messages", [])
        if not messages:
            _app.metrics["requests_error"] += 1
            return JSONResponse({"error": "No messages provided"}, status_code=400)

        # Capture live prompt identity BEFORE any modification (out-of-band only)
        _live_prompt_hash = _live_prompt_identity(messages)

        # --- Session identity ---
        sess = await _resolve_session_identity(request, messages, body)
        session_key = sess["session_key"]
        from proxy.app import _session_ctx
        _session_ctx["id"] = session_key
        rho, margin = calibrator.get_rho(session_key)
        _rho_var.set(rho)
        task_uuid = sess["uuid"]
        x_sid = sess["id"]
        _client_cap = sess["client_cap"]
        log.info("%s sid=%s session_key=%s task=%s cap=%s",
                 sess["log_label"], sess["id"], session_key, task_uuid, _client_cap)

        # Memory job — chained background init (INSERT then enqueue).
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

        _t0 = time.monotonic()
        try:
            built = await build_context(messages, task_uuid=task_uuid, session_key=session_key)
        except ContextCapacityError as cce:
            _app.metrics["requests_error"] += 1
            log.error("Context capacity: %s", cce)
            return JSONResponse({"error": {"message": str(cce), "explanation": "Protected current-turn data exceeds the safe context budget. Reduce input size or increase CTXGATE_MAX_CONTEXT."}, "ctxgate": ctxgate_meta(True, "context_capacity", 0, 0, False, 0)["ctxgate"]}, status_code=413)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: build_context %.0fms (msgs=%d)", _dt, len(messages))

        # Phase 1: Freeze seed pair [0][1][2] per session.
        if session_key not in _app.session_seeds:
            if len(built) >= 3:
                _app.session_seeds[session_key] = [dict(m) for m in built[:3]]
                log.info("Seed frozen session=%s (3 msgs)", session_key)
        elif len(built) >= 3:
            frozen = _app.session_seeds[session_key]
            for i, fm in enumerate(frozen):
                if i < len(built):
                    fc = fm.get("content", "")
                    nc = built[i].get("content", "")
                    if isinstance(fc, list):
                        fc = " ".join(p.get("text", "") for p in fc if isinstance(p, dict))
                    if isinstance(nc, list):
                        nc = " ".join(p.get("text", "") for p in nc if isinstance(p, dict))
                    if fc != nc:
                        log.warning("SEED CHANGED session=%s pos=%d - re-freezing (deliberate miss)", session_key, i)
                        _app.session_seeds[session_key] = [dict(m) for m in built[:3]]
                        _app.session_compactions.pop(session_key, None)
                        break

        # --- Phase 4: Epoch-freeze injection ---
        built = await _apply_epoch_injection(
            built, messages, session_key, task_uuid, x_sid, tenant_id)

        # Re-capture live prompt hash after injection (injection modifies last user msg)
        _live_prompt_hash = _live_prompt_identity(built)

        # Phase 2: repair dangling tool calls before fingerprint + vLLM body
        built = _repair_dangling_tool_calls(built)
        built = _with_compaction_note(built)

        # Token tracking
        _note_n = _note_tok()
        # Lazy crash recovery: if we don't have this session's exact tokens
        # in memory (post-restart), try to recover from Goose's DB.
        from proxy.hotpath.tracking import get_exact_tokens, recover_session_baseline
        if get_exact_tokens(session_key) is None:
            _recovered = recover_session_baseline(session_key)
            if _recovered is not None:
                from proxy.hotpath.tracking import store_exact_tokens
                store_exact_tokens(session_key, _recovered)
                log.info("Recovered session baseline from Goose DB: session=%s input_tokens=%d", session_key, _recovered)
        _t0 = time.monotonic()
        input_tokens = estimate_full_payload_tokens(built, body.get("tools", []), _note_n, session_key)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: count_messages_tokens %.0fms (tokens=%d)", _dt, input_tokens)
        _app.metrics["max_context_seen"] = max(_app.metrics.get("max_context_seen", 0), input_tokens)
        _app.metrics["tokens_in_total"] += input_tokens
        _track_session_tokens(session_key, input_tokens)

        # --- Cross-session knowledge extraction (fire-and-forget) ---
        if _app._EXTRACT_IN_FLIGHT < 2:
            _spawn(_fire_and_forget_extract(x_sid, session_key, messages, tenant_id=tenant_id))
        else:
            _app.metrics["extract_shed"] = _app.metrics.get("extract_shed", 0) + 1

        # Output budget
        _budget = _output_budget(input_tokens)
        if _client_cap is not None:
            if _budget < 1:
                log.error("Context leaves no room for output: session=%s input=%d budget=%d", session_key, input_tokens, _budget)
                return JSONResponse(
                    {"error": {"message": "context leaves no room for output",
                     "input_tokens": input_tokens, "budget": _budget}},
                    status_code=413)
            max_tokens = min(_budget, _client_cap)
            # MIN_OUTPUT protection: shrink input if output budget is too small
            if max_tokens < MIN_OUTPUT:
                _mo_ceiling = MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT
                log.info("Passthrough MIN_OUTPUT guard: session=%s input=%d max_tokens=%d < MIN_OUTPUT=%d ceiling=%d - shrinking",
                         session_key, input_tokens, max_tokens, MIN_OUTPUT, _mo_ceiling)
                built = _emergency_shrink(built, _mo_ceiling)
                input_tokens = estimate_full_payload_tokens(built, body.get("tools", []), _note_n, session_key)
                _budget = _output_budget(input_tokens)
                max_tokens = min(_budget, _client_cap)
                _app.metrics["min_output_shrunk"] = _app.metrics.get("min_output_shrunk", 0) + 1
            if max_tokens < MIN_OUTPUT:
                log.warning("Passthrough: max_tokens=%d < MIN_OUTPUT=%d after shrink (proceeding with reduced output)",
                            max_tokens, MIN_OUTPUT)
                _app.metrics["budget_reduced"] = _app.metrics.get("budget_reduced", 0) + 1
        else:
            max_tokens = _budget
            if max_tokens < MIN_OUTPUT:
                # Emergency shrink: ceiling guarantees MIN_OUTPUT room for output
                ceiling = MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT
                log.info("MIN_OUTPUT guard: session=%s input=%d max_tokens=%d < MIN_OUTPUT=%d ceiling=%d - shrinking",
                         session_key, input_tokens, max_tokens, MIN_OUTPUT, ceiling)
                built = _emergency_shrink(built, ceiling)
                input_tokens = estimate_full_payload_tokens(built, body.get("tools", []), _note_n, session_key)
                max_tokens = _output_budget(input_tokens)
                _app.metrics["min_output_shrunk"] = _app.metrics.get("min_output_shrunk", 0) + 1
            if max_tokens < MIN_OUTPUT:
                # MIN_OUTPUT is a target, not a hard floor.
                # Accept reduced budget as long as there's room for output.
                if max_tokens > 0:
                    log.warning("Reduced output budget: session=%s input=%d max_tokens=%d < MIN_OUTPUT=%d (proceeding)",
                                session_key, input_tokens, max_tokens, MIN_OUTPUT)
                    _app.metrics["budget_reduced"] = _app.metrics.get("budget_reduced", 0) + 1
                else:
                    # Physically impossible: input alone fills MAX_CONTEXT
                    log.error("Context fills entire MAX_CONTEXT: session=%s input=%d >= MAX_CONTEXT=%d",
                              session_key, input_tokens, MAX_CONTEXT)
                    return JSONResponse(
                        {"error": {"message": "context exceeds maximum context length",
                         "input_tokens": input_tokens, "max_context": MAX_CONTEXT}},
                        status_code=413)

        # Capacity check: if input exceeds the soft ceiling, shrink (don't reject)
        _cap_ceiling = MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT
        if input_tokens > _cap_ceiling:
            built = _emergency_shrink(built, _cap_ceiling)
            input_tokens = estimate_full_payload_tokens(built, body.get("tools", []), _note_n, session_key)
            max_tokens = _output_budget(input_tokens)
            _app.metrics["capacity_shrunk"] = _app.metrics.get("capacity_shrunk", 0) + 1
            log.info("Capacity shrink: session=%s input=%d -> %d (ceiling=%d)", session_key, input_tokens, input_tokens, _cap_ceiling)
        # Hard floor: only 413 if input alone >= MAX_CONTEXT (physically impossible)
        if input_tokens >= MAX_CONTEXT:
            _app.metrics["requests_error"] += 1
            log.error("Hard capacity exceeded: session=%s input=%d >= MAX_CONTEXT=%d", session_key, input_tokens, MAX_CONTEXT)
            return JSONResponse(
                {"error": {"type": "capacity_exceeded",
                 "message": f"Input ({input_tokens} tokens) exceeds MAX_CONTEXT ({MAX_CONTEXT}). Cannot fit.",
                 "input_tokens": input_tokens, "max_context": MAX_CONTEXT}},
                status_code=413)
        if input_tokens > 0.9 * MAX_INPUT:
            log.info("Budget: session=%s input=%d ceiling=%d max_tokens=%d",
                     session_key, input_tokens, _cap_ceiling, max_tokens)

        stream = body.get("stream", False)
        vllm_body = _build_vllm_body(built, body, max_tokens, stream, _client_cap, session_key)

        _t0 = time.monotonic()
        if _client_cap is not None:
            if stream:
                result = await stream_to_vllm_passthrough(vllm_body, input_tokens, session_key)
            else:
                result = await forward_to_vllm_passthrough(vllm_body, input_tokens, session_key)
        else:
            if stream:
                result = await stream_to_vllm(vllm_body, input_tokens, session_key, _live_prompt_hash)
            else:
                result = await forward_to_vllm(vllm_body, input_tokens, session_key, _live_prompt_hash)
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: vllm_call %.0fms (in=%d stream=%s)", _dt, input_tokens, stream)
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

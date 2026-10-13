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
from proxy.token.calibrator import calibrator
from proxy.token.counter import _rho_var
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
from proxy.hotpath.reconcile import resume_reconcile
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
    """Invalidate the calibrator's stored baseline for a session.

    After trimming/shrinking message content (not just dropping messages),
    the stored last_real baseline is stale - it reflects the pre-trim token
    count. We must force the next estimation to use the cold path (full
    re-estimation) so it reflects the actual trimmed content.
    """
    s = calibrator._sessions.get(session_key)
    if s is not None:
        s["last_real"] = 0
        s["msg_count"] = 0
    log.debug("Baseline invalidated for session=%s (post-trim)", session_key)


def _full_context_reset(messages: list) -> list:
    """Reduce to minimal viable context: system + first user + last user.

    The proxy is the sole context manager. It never asks goose to compact.
    Nuclear option when progressive trimming has failed.
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return messages[:1] if messages else messages
    result = list(system_msgs)
    result.append(user_msgs[0])  # first user msg = task origin
    if len(user_msgs) > 1:
        result.append(user_msgs[-1])  # last user msg = current prompt
    return result


def _aggressive_trim(built: list, target_tokens: int, session_key: str,
                   total_input_tokens: int | None = None) -> list:
    """Aggressively trim a built context to fit within target_tokens.

    When total_input_tokens is provided (including injected context overhead),
    the effective target for raw message tokens is reduced by that overhead.
    This ensures trimming accounts for the FULL payload size, not just messages.

    Strategy:
    1. Try emergency_shrink with aggressive=True (drops tool groups)
    2. If still over, try with allow_injection_prune=True
    3. If still over, truncate to system + last user message only

    After trimming, invalidates the calibrator baseline so re-estimation
    uses the cold path (actual content size, not stale baseline).
    """
    # Account for injected context: the target must be met by RAW message
    # tokens such that raw + injected <= target_tokens overall.
    inject_overhead = 0
    if total_input_tokens is not None:
        msg_tok_now = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]
        inject_overhead = max(0, total_input_tokens - msg_tok_now)
    effective_target = max(1, target_tokens - inject_overhead)

    current_tokens = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]
    if current_tokens <= effective_target:
        # Even though messages fit (after accounting for injection),
        # the calibrator baseline may be stale (e.g., from a pre-shrink
        # vLLM response). Invalidate it so the next
        # exact_count_messages always uses the real tokenizer.
        _invalidate_baseline(session_key)
        return built

    log.info("AGGRESSIVE TRIM: session=%s current=%d eff_target=%d (target=%d inject_ovhd=%d) msgs=%d",
             session_key, current_tokens, effective_target, target_tokens, inject_overhead, len(built))

    # Step 1: aggressive emergency shrink (unprotects tool groups)
    try:
        trimmed = _emergency_shrink(built, effective_target, aggressive=True)
        trimmed_tokens = exact_count_messages(trimmed, VLLM_MODEL)  # [RULE 3: post-cut]
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
        trimmed_tokens = exact_count_messages(trimmed, VLLM_MODEL)  # [RULE 3: post-cut]
        if trimmed_tokens <= effective_target:
            log.info("AGGRESSIVE TRIM: success (injection_prune) %d -> %d tokens (eff_target=%d)",
                     current_tokens, trimmed_tokens, effective_target)
            _invalidate_baseline(session_key)
            return trimmed
    except ContextCapacityError:
        pass

    # Step 3: nuclear - minimal viable context (system + first user + last user + memories)
    log.warning("AGGRESSIVE TRIM: nuclear (minimal viable) session=%s", session_key)
    system_msgs = [m for m in built if m.get("role") == "system"]
    user_msgs = [m for m in built if m.get("role") == "user"]
    trimmed = list(system_msgs)
    if user_msgs:
        trimmed.append(user_msgs[0])  # first user msg = task origin
        if len(user_msgs) > 1:
            trimmed.append(user_msgs[-1])  # last user msg = current prompt
    else:
        trimmed = built[:1] if built else []
    # token count omitted per 3-rule policy (logging only)
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


async def _try_full_reset_and_forward(built, body, input_tokens, session_key, _client_cap, _live_prompt_hash, _disable_thinking, max_tokens):
    """Last-resort: full context reset and forward to vLLM. Returns response or None."""
    import time as _time
    _minimal = _full_context_reset(built)
    _reset_tokens = exact_count_messages(_minimal, VLLM_MODEL)  # [RULE 2: pre-vLLM]
    _reset_max = MAX_OUTPUT
    log.warning("FULL RESET: session=%s msgs %d->%d in=%d max=%d",
                session_key, len(built), len(_minimal), _reset_tokens, _reset_max)
    _vllm_body = _build_vllm_body(_minimal, body, _reset_max, False, _client_cap, session_key, disable_thinking=True)
    _t0 = _time.monotonic()
    result = await _forward_to_vllm(_vllm_body, _reset_tokens, session_key, _client_cap, _live_prompt_hash)
    _dt = (_time.monotonic() - _t0) * 1000
    if _dt > 50:
        log.warning("SLOW: vllm(full_reset) %.0fms in=%d", _dt, _reset_tokens)
    if result is not None and not _is_empty_completion(result):
        log.info("FULL RESET ok: session=%s", session_key)
        return result
    log.error("FULL RESET failed: session=%s", session_key)
    return None


async def chat_completions(request: Request):
    import proxy.app as _app
    global _inflight_count, _shutting_down
    _inflight_count += 1
    _sem_acquired = False
    _sem = None
    try:
        if _shutting_down:
            # Drain mode: still process (inflight counter ensures clean exit)
            log.info("Shutdown drain: processing request")

        # Phase 2: tenant_id from auth dependency
        tenant_id = getattr(request.state, "tenant_id", "noauth")

        # Phase 2: per-tenant rate limiting (skipped when rate is 0)
        if CTXGATE_TENANT_RATE_PER_SEC > 0:
            _tl = _get_tenant_rate_limiter(tenant_id)
            _tl._refill()
            if _tl.tokens < 1:
                # Don't reject - wait for a token (max 5s)
                _deficit = 1 - _tl.tokens
                _wait = min(5.0, _deficit / max(_tl.rate, 0.1) + 0.1)
                log.info("Rate limit: tenant=%s waiting %.1fs", tenant_id, _wait)
                import time as _t_mod
                _t_mod.sleep(_wait)
                _tl._refill()
            _tl.tokens -= 1

        # Phase 2: per-tenant concurrency semaphore (skipped when 0)
        if CTXGATE_TENANT_MAX_CONCURRENCY > 0:
            _sem = _get_tenant_semaphore(tenant_id)
            _sem_acquired = False
            try:
                await asyncio.wait_for(_sem.acquire(), timeout=30)
                _sem_acquired = True
            except asyncio.TimeoutError:
                log.warning("CONCURRENCY LIMIT REACHED: tenant=%s sem_full after 30s", tenant_id)
                metrics["concurrency_limit_reached"] = metrics.get("concurrency_limit_reached", 0) + 1
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
            except asyncio.TimeoutError:
                # Proceed without slot - serve slowly rather than reject
                log.warning("Concurrency: tenant=%s 60s timeout - proceeding", tenant_id)

        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            # Let through - build_context will trim to fit
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
            # Transparent routing to the actual model
            log.info("Model %r -> %r", model, VLLM_MODEL)
            body["model"] = VLLM_MODEL
            model = VLLM_MODEL

        messages = body.get("messages", [])
        if not messages:
            _app.metrics["requests_error"] += 1
            log.error("No messages in request")
            return _minimal_valid_response("no_messages", content="No messages in request. Please resend.")

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
            _goose_toks = body.get("usage", {}).get("total_tokens")
            built, _toks = await build_context(messages, task_uuid=task_uuid, session_key=session_key, goose_reported_tokens=_goose_toks)
        except ContextCapacityError as cce:
            log.warning("Context capacity: %s - full reset", cce)
            _reset_msgs = _full_context_reset(messages)
            try:
                built, _toks = await build_context(_reset_msgs, task_uuid=task_uuid, session_key=session_key, goose_reported_tokens=_goose_toks)
                log.info("Capacity recovered via full reset: session=%s", session_key)
                _invalidate_baseline(session_key)
            except ContextCapacityError as cce2:
                log.error("Capacity unrecoverable via reset: %s", cce2)
                # Last-resort recovery: hard-cut/truncate messages to fit budget
                try:
                    _safe_max = MAX_INPUT
                    _orig_tokens = exact_count_messages(_reset_msgs, VLLM_MODEL)  # [RULE 2: pre-vLLM]
                    _msgs_copy = [dict(m) for m in _reset_msgs]
                    _hard_cut_to(_msgs_copy, _orig_tokens, _safe_max)
                    # If hard cut removed whole messages but content is still over,
                    # truncate content proportionally (preserves beginning)
                    _post_tokens = exact_count_messages(_msgs_copy, VLLM_MODEL)  # [RULE 3: post-cut]
                    if _post_tokens > _safe_max and _msgs_copy:
                        _ratio = (_safe_max * 0.95) / _post_tokens
                        for _m in _msgs_copy:
                            _c = _m.get("content", "")
                            if isinstance(_c, str) and len(_c) > 100:
                                _m["content"] = _c[:int(len(_c) * _ratio)]
                        _post_tokens = exact_count_messages(_msgs_copy, VLLM_MODEL)  # [RULE 3: post-cut]
                    if _post_tokens <= _safe_max and any(
                        m.get("content") for m in _msgs_copy):
                        log.warning(
                            "SINGLE_MSG_RECOVERY: session=%s original=%d tokens, truncated to ~%d",
                            session_key, _orig_tokens, _post_tokens)
                        built = _msgs_copy
                        _invalidate_baseline(session_key)
                    else:
                        raise ValueError("Hard cut produced empty/too-small result")
                except Exception as _recovery_err:
                    log.error("Single-msg recovery failed: %s", _recovery_err)
                    return _minimal_valid_response("context_capacity",
                        content="Context too large even after reset. Please start a new session.")
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: build_context %.0fms (msgs=%d)", _dt, len(messages))

        # --- Resume-time reconciliation (Problem B fix) ---
        # Detect: first request for this session after proxy restart
        # (get_exact_tokens is None AND window state was loaded from DB)
        from proxy.hotpath.tracking import get_exact_tokens
        _is_resume = get_exact_tokens(session_key) is None
        _ws_loaded = _app.session_compactions.get(session_key)
        if _is_resume and _ws_loaded is not None:
            _adapted, _was_adapted, _reconcile_action = resume_reconcile(
                session_key, built, body, _ws_loaded)
            if _was_adapted:
                log.info("RECONCILE: session=%s action=%s", session_key, _reconcile_action)
                built = _adapted
                # Reset the 413 counter since we've adapted
                _413_counts.pop(session_key, None)
                # Invalidate baseline after reconciliation (content changed)
                _invalidate_baseline(session_key)

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
        # Capture pre-injection token count for truthful WINDOW log
        # pre-injection token count omitted per 3-rule policy
        # _app._pre_injection_tokens omitted per 3-rule policy
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
        # SINGLE SOURCE OF TRUTH: input_tokens is computed AFTER injection
        input_tokens = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: exact_count_messages %.0fms (tokens=%d)", _dt, input_tokens)
        _app.metrics["max_context_seen"] = max(_app.metrics.get("max_context_seen", 0), input_tokens)
        _app.metrics["tokens_in_total"] += input_tokens
        _track_session_tokens(session_key, input_tokens)

        # --- Cross-session knowledge extraction (fire-and-forget) ---
        if _app._EXTRACT_IN_FLIGHT < 2:
            _spawn(_fire_and_forget_extract(x_sid, session_key, messages, tenant_id=tenant_id))
        else:
            _app.metrics["extract_shed"] = _app.metrics.get("extract_shed", 0) + 1

        # --- Stable thinking flag ---
        # The _disable_thinking decision is made ONCE per turn and persisted
        # in the window state. It only changes when a new user message arrives.
        _ws = _app.session_compactions.get(session_key)
        _disable_thinking = False
        if _ws is not None:
            # Reuse stored value if we're in the same turn
            _stored_thinking = _ws.get("thinking_disabled")
            if _stored_thinking is not None:
                _disable_thinking = _stored_thinking
            # New turn detection: if the live prompt hash changed, it's a new turn
            _current_live_hash = _live_prompt_hash
            _stored_live_hash = _ws.get("live_prompt_hash")
            if _stored_live_hash != _current_live_hash:
                # New turn: recompute and store
                _disable_thinking = False  # will be set below if needed
                _ws["live_prompt_hash"] = _current_live_hash

        # Proactive thinking disable: when input exceeds MAX_INPUT, the model
        # needs its full output budget for content. With thinking ON, it wastes
        # ~90% of that budget on internal reasoning, leaving goose an empty response.
        # Disable thinking so the model produces actual content.
        # Disable thinking early: when input + expected reasoning tokens would
        # consume most of the output budget, leave no room for visible content.
        # Trigger threshold = MAX_INPUT - MAX_REASONING_TOKENS, so thinking is
        # off before the model burns its entire budget on internal reasoning.
        _think_trigger = MAX_INPUT
        if not _disable_thinking and input_tokens > _think_trigger:
            _disable_thinking = True
            log.info("THINKING DISABLED: session=%s input=%d > trigger=%d (MAX_INPUT=%d - REASONING=%d)",
                     session_key, input_tokens, _think_trigger, MAX_INPUT, MAX_REASONING_TOKENS)
        
        # Output budget
        # When thinking is OFF, use full MAX_OUTPUT (respecting goose's request)
        # but cap by remaining context room to never exceed MAX_CONTEXT.
        # When thinking is ON, use _output_budget which accounts for thinking token cost.
        if _disable_thinking:
            _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
        else:
            _budget = _output_budget(input_tokens)

        # === HARD CEILING ENFORCEMENT: never admit input exceeding safe budget ===
        # MAX_INPUT is the authoritative input ceiling.
        # If estimated input exceeds it, trim BEFORE the proactive trim logic below.
        _safe_max_input = MAX_INPUT
        if input_tokens > _safe_max_input:
            log.warning("HARD CEILING: session=%s input=%d exceeds safe_max=%d, trimming",
                        session_key, input_tokens, _safe_max_input)
            _app.metrics["hard_ceiling_trim"] = _app.metrics.get("hard_ceiling_trim", 0) + 1
            # DISABLED:            trimmed = _aggressive_trim(built, _safe_max_input, session_key, input_tokens)
            # DISABLED:            built = trimmed
            input_tokens = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]
        
        # === PROACTIVE TRIM: ensure input + output fits in context window ===
        # This is the KEY fix: BEFORE sending to vLLM, verify that
        # input_tokens + max_tokens <= MAX_CONTEXT. If not, trim aggressively.
        if _budget < 1:
            log.warning("PROACTIVE TRIM: session=%s input=%d leaves no output room (budget=%d)",
                        session_key, input_tokens, _budget)
            # Calculate how many input tokens we need to trim to free up MAX_OUTPUT
            # Need: input_tokens + MAX_OUTPUT + SAFETY_MARGIN <= MAX_CONTEXT
            # So: input_tokens <= MAX_CONTEXT - MAX_OUTPUT - SAFETY_MARGIN
            max_allowed_input = MAX_CONTEXT - MAX_OUTPUT - SAFETY_MARGIN
            _app.metrics["proactive_trim"] = _app.metrics.get("proactive_trim", 0) + 1

            # Step 1: aggressive emergency shrink
            # DISABLED:            trimmed = _aggressive_trim(built, max_allowed_input, session_key, input_tokens)
            # DISABLED:            built = trimmed
            # Re-estimate with invalidated baseline (cold path = actual content)
            input_tokens = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]

            if _disable_thinking:
                _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
            else:
                _budget = _output_budget(input_tokens)

            # Step 2: if STILL no room, nuclear reset
            if _budget < 1:
                log.warning("PROACTIVE TRIM: nuclear reset session=%s input=%d", session_key, input_tokens)
                _minimal = _full_context_reset(built)
                built = _minimal
                _invalidate_baseline(session_key)
                input_tokens = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]
                if _disable_thinking:
                    _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
                else:
                    _budget = _output_budget(input_tokens)

            # Final check: if truly no room, truncate content to fit
            if _budget < 1:
                log.warning("PROACTIVE TRIM: nuclear reset insufficient - attempting content truncation: session=%s input=%d max_ctx=%d",
                            session_key, input_tokens, MAX_CONTEXT)
                # Truncate message content proportionally to fit within max_allowed_input
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
                    input_tokens = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]
                    if _disable_thinking:
                        _budget = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
                    else:
                        _budget = _output_budget(input_tokens)
                    log.warning("PROACTIVE TRIM: content truncated session=%s new_input=%d budget=%d",
                                session_key, input_tokens, _budget)
                if _budget < 1:
                    log.error("PROACTIVE TRIM: even content truncation leaves no room: session=%s input=%d max_ctx=%d",
                              session_key, input_tokens, MAX_CONTEXT)
                    _budget = 1  # Absolute last resort
                    _disable_thinking = True
                    if _ws is not None:
                        _ws["thinking_disabled"] = True

            log.info("PROACTIVE TRIM: resolved session=%s input=%d budget=%d", session_key, input_tokens, _budget)

        max_tokens = min(_budget, _client_cap) if _client_cap is not None else _budget
        if max_tokens < 1:
            max_tokens = 1
        if max_tokens < MAX_OUTPUT and _client_cap is None:
            # Only bump to MAX_OUTPUT if the client didn't cap it
            # and we have room (input + MAX_OUTPUT + margin <= MAX_CONTEXT)
            if input_tokens + MAX_OUTPUT + SAFETY_MARGIN <= MAX_CONTEXT:
                max_tokens = MAX_OUTPUT

        if _disable_thinking and _ws is not None:
            _ws["thinking_disabled"] = True

        # --- Log the window state ---
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
        log.warning(
            "WINDOW: total=%d kept=%d(%ds+%dn) drop=%d | cut=%s | prefix=%s | raw=%d in=%d injected=%d out=%d think=%s",
            _total_msgs, _kept, _stable, _new, _msg_counts.get("dropped", _total_msgs - _kept), _ctx_path, _prefix,
            _raw_tokens, input_tokens, _injected_delta, max_tokens, _think_str,
        )

        # --- Send to vLLM with empty-response retry ---
        stream = body.get("stream", False)
        vllm_body = _build_vllm_body(built, body, max_tokens, stream, _client_cap, session_key, disable_thinking=_disable_thinking)

        result = await _forward_to_vllm(vllm_body, input_tokens, session_key, _client_cap, _live_prompt_hash, stream)

        # --- Empty response handling: trim more and retry ---
        # Only applies to non-streaming responses (streaming can't be checked post-hoc)
        if result is not None and not stream and _is_empty_completion(result):
            log.warning("EMPTY RESPONSE: session=%s input=%d max=%d - trimming and retrying",
                        session_key, input_tokens, max_tokens)
            _app.metrics["empty_response_retries"] = _app.metrics.get("empty_response_retries", 0) + 1

            # Trim more aggressively: reduce input to leave more room for output
            max_allowed_input = MAX_CONTEXT - MAX_OUTPUT - SAFETY_MARGIN
            trimmed = _aggressive_trim(built, max_allowed_input, session_key, input_tokens)
            if trimmed is not built:  # Actually trimmed something
                built = trimmed
                input_tokens = exact_count_messages(built, VLLM_MODEL)  # [RULE 2: pre-vLLM]
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

        # === FINISH_REASON=LENGTH HANDLING: deliver partial content with marker ===
        if result is not None and not stream and isinstance(result, JSONResponse):
            try:
                import json as _json_fr
                _fr_data = _json_fr.loads(result.body)
                _fr_choices = _fr_data.get('choices', [])
                if _fr_choices:
                    _fr = _fr_choices[0].get('finish_reason')
                    if _fr == 'length':
                        log.warning('FINISH_REASON_LENGTH: session=%s entering shrink-retry loop', session_key)
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
                                # Case 1: SUCCESS
                                log.info('FINISH_RETRY_SUCCESS: session=%s attempt=%d ew_total=%d limit=%d',
                                         session_key, _attempt+1, _ew_total, _limit)
                                _app.metrics['finish_length_success'] = _app.metrics.get('finish_length_success', 0) + 1
                                _success = True
                                break

                            elif _ew_total <= _limit and _resp_fr == 'length':
                                # Case 2: Tokens OK but still truncated - just retry (model may vary)
                                log.info('FINISH_RETRY_RESUBMIT: session=%s attempt=%d ew_total=%d (under limit, retrying)',
                                         session_key, _attempt+1, _ew_total)
                                _retry_result = await _forward_to_vllm(vllm_body, input_tokens, session_key, _client_cap, _live_prompt_hash)
                                if _retry_result is None:
                                    continue
                                result = _retry_result

                            elif _ew_total > _limit and _resp_fr == 'length':
                                # Case 3: Over limit AND truncated - emergency cut first, then retry
                                log.warning('FINISH_RETRY_SHRINK: session=%s attempt=%d ew_total=%d > limit=%d',
                                           session_key, _attempt+1, _ew_total, _limit)
                                _ceiling = MAX_INPUT - 1024
                                _shrunken = _emergency_shrink(_cont_msgs, _ceiling, aggressive=True)

                                if len(_shrunken) >= len(_cont_msgs):
                                    log.warning('FINISH_RETRY_EXHAUSTED: cannot shrink further, session=%s', session_key)
                                    break

                                _cont_msgs = _shrunken
                                # [RULE 3: post-cut] Re-tokenize with REAL tokenizer after emergency cut
                                input_tokens = exact_count_messages(_cont_msgs, VLLM_MODEL)
                                max_tokens = min(MAX_OUTPUT, MAX_CONTEXT - input_tokens - SAFETY_MARGIN)
                                if max_tokens < 1:
                                    max_tokens = 1

                                vllm_body = _build_vllm_body(_cont_msgs, body, max_tokens, False, _client_cap, session_key, disable_thinking=_disable_thinking)

                                _retry_result = await _forward_to_vllm(vllm_body, input_tokens, session_key, _client_cap, _live_prompt_hash)
                                if _retry_result is None:
                                    continue
                                result = _retry_result

                            else:
                                # Case 4: Over limit but NOT truncated (edge case) - emergency cut
                                log.warning('FINISH_RETRY_OVERLIMIT: session=%s attempt=%d ew_total=%d > limit=%d fr=%s',
                                           session_key, _attempt+1, _ew_total, _limit, _resp_fr)
                                _ceiling = MAX_INPUT - 1024
                                _shrunken = _emergency_shrink(_cont_msgs, _ceiling, aggressive=True)

                                if len(_shrunken) >= len(_cont_msgs):
                                    break

                                _cont_msgs = _shrunken
                                # [RULE 3: post-cut] Re-tokenize with REAL tokenizer after emergency cut
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
                            # Exhausted all retries - deliver with TRUNCATED marker as final fallback
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
                pass  # Non-fatal: deliver as-is if parsing fails
        

            # If STILL empty after retry, try full context reset
            if result is not None and _is_empty_completion(result):
                log.warning("EMPTY RESPONSE PERSISTS: session=%s - trying full reset", session_key)
                _rr = await _try_full_reset_and_forward(built, body, input_tokens, session_key, _client_cap, _live_prompt_hash, True, MAX_OUTPUT)
                if _rr is not None:
                    result = _rr

            # Last resort: if vLLM truly returns nothing, return a minimal valid completion
            if result is not None and _is_empty_completion(result):
                log.error("EMPTY RESPONSE UNRECOVERABLE: session=%s - returning minimal completion", session_key)
                _app.metrics["empty_response_unrecoverable"] = _app.metrics.get("empty_response_unrecoverable", 0) + 1
                result = _minimal_valid_response("empty_response", model=VLLM_MODEL,
                    content="I'm having difficulty processing that request. Could you please rephrase it?")

        # Reset 413 counter on successful (non-empty) response
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


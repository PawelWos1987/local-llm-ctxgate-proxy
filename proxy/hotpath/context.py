"""Context building: window management, message preparation, system message normalization."""
import asyncio
import logging
import time

from proxy.config import STABLE_ELIDE, MAX_INPUT, MAX_CONTEXT, SAFETY_MARGIN, MIN_OUTPUT
from proxy.token.calibrator import calibrator
from proxy.token.counter import _rho_var
from proxy.token import count_messages_tokens
from proxy.window import (
    _window_valid, _newest_user_idx, _stable_elide_idx, _stable_elide_inplace,
    _seed_sig, _new_window_state, _recut, _kept_messages,
    _emergency_shrink, ContextCapacityError, verify_context_invariants,
    _window_load, _eviction_trigger, _trim_target,
)
from proxy.summarize.trim_summary import _summarize_trimmed_messages
from proxy.lifecycle.recovery import _spawn
from proxy.utils.helpers import _note_tok

log = logging.getLogger("ctxgate-proxy")


def _window_lock(session_key: str) -> asyncio.Lock:
    import proxy.app as _app
    lk = _app._window_locks.get(session_key)
    if lk is None:
        lk = asyncio.Lock()
        _app._window_locks[session_key] = lk
    return lk


def _prep_messages(raw: list) -> list:
    from proxy.hotpath.messaging import strip_reasoning
    from proxy.app import metrics

    out = []
    for m in strip_reasoning(raw):
        if m.get("content") is None:
            m["content"] = ""
        tc = m.get("tool_calls")
        if tc:
            valid = True
            for t in tc:
                if not isinstance(t, dict) or "id" not in t or "type" not in t or "function" not in t:
                    valid = False; break
                fn = t.get("function", {})
                if "name" not in fn or "arguments" not in fn:
                    valid = False; break
                a = fn["arguments"]
                if isinstance(a, str):
                    try:
                        import json
                        json.loads(a)
                    except (json.JSONDecodeError, ValueError):
                        valid = False; break
                elif not isinstance(a, dict):
                    valid = False; break
            if not valid:
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                if not m.get("content"): m["content"] = ""
                metrics["toolcall_strips"] += 1
        out.append(m)
    if not any(m.get("role") == "user" for m in out):
        out.append({"role": "user", "content": "Please proceed."})
    return out


def _normalize_system_messages(messages):
    if not messages:
        return messages
    # Never convert a non-system slot into a system one.
    if messages[0].get("role") != "system":
        return messages
    sys_parts = []
    rest = []
    for m in messages:
        if m.get('role') == 'system':
            c = m.get('content')
            if c:
                sys_parts.append(c)
        else:
            rest.append(m)
    if not sys_parts:
        return messages
    res = [dict(messages[0])]
    res[0]['role'] = 'system'
    res[0]['content'] = (chr(10) + chr(10) + chr(10)).join(sys_parts)
    res.extend(rest)
    return res


async def build_context(request_messages: list, task_uuid: str = None, session_key: str = None) -> list:
    import proxy.app as _app
    from proxy.app import _inject_ledger_into_context

    sk = session_key or ""
    rho, margin = calibrator.get_rho(sk)
    _rho_var.set(rho)
    _note_n = _note_tok()  # tokens reserved for COMPACTION_NOTE (added after build_context)
    async with _window_lock(sk):
        ws = _app.session_compactions.get(sk)
        if ws is None and sk and sk not in _app._window_load_tried:
            _app._window_load_tried.add(sk)
            loaded = await _window_load(sk)
            if loaded:
                ws = loaded
                _app.session_compactions[sk] = ws
                _app.metrics["window_loads_db"] += 1
        raw = request_messages
        if raw and raw[0].get("role") == "system":
            seed_raw = [raw[0]]
            rest_raw = raw[1:]
        else:
            seed_raw = []
            rest_raw = list(raw)
        # FAST PATH: valid sticky cut -> O(kept) only; never tokenizes the dropped region.
        if ws and _window_valid(ws, seed_raw, rest_raw) and ws["cut"] < len(rest_raw):
            # If the live prompt (newest user msg) is before the cut, extend
            # the effective cut to include it at its natural position.
            lu = _newest_user_idx(rest_raw)
            eff_cut = ws["cut"]
            if lu is not None and lu < eff_cut:
                eff_cut = lu
            combined = _prep_messages(seed_raw + rest_raw[eff_cut:])
            kept = list(combined)
            if STABLE_ELIDE:
                if ws.get("elide_idx") is None:
                    ws["elide_idx"] = _stable_elide_idx(eff_cut, rest_raw)
                # Deterministically elide the [eff_cut, elide_idx) tool bodies so
                # the kept window is byte-stable between requests.
                # kept = seed + rest[eff_cut:]
                # rest[eff_cut] starts at kept[len(seed)]
                base = len(seed_raw)
                _stable_elide_inplace(kept, base, base + (ws["elide_idx"] - eff_cut))
            kept_tok = count_messages_tokens(kept)
            ceiling = min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT) - _note_n
            trigger = _eviction_trigger()
            if kept_tok > ceiling:
                # Hard ceiling exceeded: must re-cut (capacity risk).
                log.info('sticky window over hard ceiling %d > %d: re-cutting', kept_tok, ceiling)
            elif kept_tok > trigger:
                # High watermark crossed: hysteresis re-cut to target.
                log.info('sticky window over trigger %d > %d: hysteresis re-cut to target %d',
                         kept_tok, trigger, _trim_target())
            elif kept_tok <= MAX_INPUT - _note_n:
                _app.metrics["trim_sticky_reuse"] += 1
                log.info("Context sticky-reuse: session=%s cut=%d kept_tokens=%d skipped=%d", sk, ws["cut"], kept_tok, ws["cut"])
                # Verify context invariants - HARD GATE on Goose path
                _viols = verify_context_invariants(kept, raw, ceiling, kept_tok)
                if _viols:
                    _fatal_viols = []
                    for _v in _viols:
                        if _v.startswith("newest_user") or _v.startswith("pinned"):
                            _app.metrics["pinned_user_preservation_failures"] += 1
                            _fatal_viols.append(_v)
                        elif _v.startswith("orphan_tool") or _v.startswith("missing_tool_result"):
                            _app.metrics["recent_tool_preservation_failures"] += 1
                            _fatal_viols.append(_v)
                        elif _v.startswith("tool_body"):
                            _app.metrics["recent_tool_preservation_failures"] += 1
                    if _fatal_viols:
                        log.error("Context invariant HARD GATE (fast path): %s", _fatal_viols[:3])
                        raise ContextCapacityError(
                            f"context invariant violation in fast path: {_fatal_viols[0]}",
                            kept_tok, ceiling)
                if task_uuid and ws["summarized_through"] < ws.get("dropped_total", ws["cut"]) and not ws.get("in_flight", False):
                    la = _app._summary_last_attempt.get(task_uuid)
                    if la is None or (time.time() - la[0]) > 120:
                        ws["pending_cut"] = ws["cut"]
                        ws["in_flight"] = True
                        _spawn(_summarize_trimmed_messages(task_uuid, sk, rest_raw, pending_cut=ws["cut"]))
                kept = _inject_ledger_into_context(kept, task_uuid, sk, "")
                return kept
        # SLOW PATH: no valid state (or kept over limit) -> full count + re-cut.
        messages = _prep_messages(raw)
        if messages and messages[0].get("role") == "system":
            seed = [messages[0]]
            rest = messages[1:]
        else:
            seed = []
            rest = list(messages)
        total = count_messages_tokens(messages)
        log.info("Context: %d messages, %d tokens (limit %d)", len(messages), total, MAX_INPUT)
        if total <= MAX_INPUT - _note_n:
            return messages
        target = _trim_target()
        # Pre-recut elision: shrink tool bodies up to the newest-4 boundary so
        # _recut_to budgets on stable (post-elision) sizes, not full originals.
        # messages is a fresh _prep_messages list (dict copies) -> caller safe.
        _pre_elide = _stable_elide_idx(0, rest) if STABLE_ELIDE else 0
        if STABLE_ELIDE:
            # messages = seed(3) + rest; _pre_elide is a rest-index.
            # Elide rest[0:_pre_elide] -> messages[3 : 3+_pre_elide]
            _stable_elide_inplace(messages, 3, 3 + _pre_elide)
        cut = _recut(messages)
        elide_idx = max(cut, _pre_elide) if STABLE_ELIDE else 0
        # F2b: never store a window anchored past the end of rest (caused the
        # per-turn re-collapse, W2). Clamp so the anchor is a real message.
        if rest and cut >= len(rest):
            cut = len(rest) - 1
        # F3: carry the summarizer watermark forward on a re-cut of the SAME session
        # (seed unchanged, history only grew) - never reset to 0. Only a genuinely
        # new/changed seed (or missing state) starts from 0.
        st_start = 0
        if ws:
            if _window_valid(ws, seed_raw, rest_raw):
                st_start = ws.get("summarized_through", 0)
            elif ws.get("seed_sig") == _seed_sig(seed):
                st_start = ws.get("summarized_through", 0)
        new_ws = _new_window_state(cut, rest, seed, st_start)
        _app.session_compactions[sk] = new_ws
        _app.metrics["trim_events"] += 1
        _app.metrics["dropped_total"] += (cut - st_start) if cut > st_start else 0
        _app.metrics["compaction_events"] += 1
        kept = _kept_messages(seed, rest, cut)
        new_ws["elide_idx"] = elide_idx
        if STABLE_ELIDE:
            # Elide the [cut, elide_idx) tool bodies in the freshly built kept
            # window. kept = seed + rest[cut:]
            base2 = len(seed)
            _stable_elide_inplace(kept, base2, base2 + (elide_idx - cut))
        kept_tok = count_messages_tokens(kept)
        ceiling = min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MIN_OUTPUT) - _note_n
        if kept_tok > ceiling:
            try:
                kept = _emergency_shrink(kept, ceiling)
            except ContextCapacityError as cce:
                log.error("Context capacity exceeded in build_context: %s", cce)
                raise cce
            kept_tok = count_messages_tokens(kept)
        if kept_tok > ceiling:
            log.error("EMERGENCY SHRINK failed to meet ceiling: kept_tok=%d ceiling=%d (pathological single huge message)", kept_tok, ceiling)
        # Verify context invariants - HARD GATE on Goose path
        _viols = verify_context_invariants(kept, raw, ceiling, kept_tok)
        if _viols:
            _fatal_viols = []
            for _v in _viols:
                if _v.startswith("newest_user") or _v.startswith("pinned"):
                    _app.metrics["pinned_user_preservation_failures"] += 1
                    _fatal_viols.append(_v)
                elif _v.startswith("orphan_tool") or _v.startswith("missing_tool_result"):
                    _app.metrics["recent_tool_preservation_failures"] += 1
                    _fatal_viols.append(_v)
                elif _v.startswith("tool_body"):
                    _app.metrics["recent_tool_preservation_failures"] += 1
            if _fatal_viols:
                log.error("Context invariant HARD GATE (slow path): %s", _fatal_viols[:3])
                raise ContextCapacityError(
                    f"context invariant violation in slow path: {_fatal_viols[0]}",
                    kept_tok, ceiling)
        headroom = ceiling - kept_tok
        log.info("Context over limit: %d > %d, re-cut to low-watermark %d (%.0f%%)", total, MAX_INPUT, target, 100.0 * target / MAX_INPUT)
        log.info("After trim: %d msgs, %d tokens (headroom %d ceiling %d)", len(kept), kept_tok, headroom, ceiling)
        if kept_tok > target:
            log.info("target missed by %d tokens", kept_tok - target)
        if cut > st_start and task_uuid:
            log.info("Dropped slice: session=%s msgs=%d -> summarizer", sk, cut - st_start)
            new_ws["pending_cut"] = cut
            new_ws["in_flight"] = True
            _spawn(_summarize_trimmed_messages(task_uuid, sk, rest, pending_cut=cut))
        kept = _inject_ledger_into_context(kept, task_uuid, sk, "")
        return kept

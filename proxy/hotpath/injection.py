"""Epoch-freeze injection: knowledge, task memory, and session digest."""
import asyncio
import logging
import time

from proxy.config import (
    CTXGATE_INJECT_EPOCH_FREEZE_ON, CTXGATE_INJECT_MAX_TOKENS_CFG,
    CTXGATE_INJECT_MAX_TOKENS_RECAP_CFG, MAX_INPUT, MAX_CONTEXT,
    SAFETY_MARGIN, MAX_OUTPUT,
)
from proxy.window.state import _msg_anchor
from proxy.utils.helpers import _detect_recap_intent, _compute_epoch_key, _fit_to_budget
from proxy.memory import fetch_task_memory
from proxy.lifecycle.injection import _record_injection

log = logging.getLogger("ctxgate-proxy")


async def _apply_epoch_injection(
    built: list, messages: list, session_key: str,
    task_uuid: str, x_sid: str, tenant_id: str,
) -> list:
    """Apply epoch-freeze injection to the built context.

    Computes (or reuses) the injection block (knowledge + task memory + digest)
    and appends it to the last user message. Returns the modified built list.
    """
    import proxy.app as _app
    from proxy.summarize.digest import _build_session_digest

    tm = ""
    kn = ""
    _epoch_freeze = CTXGATE_INJECT_EPOCH_FREEZE_ON

    if _epoch_freeze:
        # Compute epoch key
        _raw_last_user_e = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        _newest_user_anchor = _msg_anchor(_raw_last_user_e) if _raw_last_user_e else ""
        _recap = _detect_recap_intent(
            _raw_last_user_e.get("content", "") if _raw_last_user_e and isinstance(_raw_last_user_e.get("content"), str) else ""
        )
        _ws_for_epoch = _app.session_compactions.get(session_key)
        _epoch_key = _compute_epoch_key(session_key, _ws_for_epoch, _newest_user_anchor, _recap)

        # Check if we have a cached block for this epoch
        _cached_block = _ws_for_epoch.get("injected_block") if _ws_for_epoch else None
        _cached_epoch = _ws_for_epoch.get("injected_epoch") if _ws_for_epoch else None

        if _cached_epoch == _epoch_key and _cached_block is not None:
            # Reuse the exact same block (byte-for-byte)
            kn = ""
            tm = ""
            _block = _cached_block
            log.debug("Epoch freeze: reusing cached injection block (%d chars) for %s", len(_block), _epoch_key[:40])
        else:
            # New epoch: compute the block
            _t0 = time.monotonic()
            kn = ""  # knowledge injection disabled
            tm, _digest = await asyncio.gather(
                fetch_task_memory(x_sid, built, task_uuid=task_uuid, recap=_recap, tenant_id=tenant_id),
                _build_session_digest(task_uuid, session_key, tenant_id=tenant_id),
                return_exceptions=True
            )
            _dt = (time.monotonic() - _t0) * 1000
            if _dt > 50:
                log.warning("SLOW: knowledge+memory fetch %.0fms", _dt)
            if isinstance(kn, Exception):
                log.warning("Knowledge injection failed: %s", kn)
                kn = ""
            if isinstance(tm, Exception):
                log.warning("Task memory injection failed: %s", tm)
                tm = ""
            if isinstance(_digest, Exception):
                log.debug("Digest fetch failed: %s", _digest)
                _digest = ""

            # Assemble the block
            _ctx_parts = []
            if kn:
                _ctx_parts.append("Relevant knowledge:" + chr(10) + kn)
            if tm:
                _ctx_parts.append("Task memory:" + chr(10) + tm)
            if _digest:
                _ctx_parts.append("Session digest:" + chr(10) + _digest)

            # Phase 4: budget the injection block
            _inject_budget = CTXGATE_INJECT_MAX_TOKENS_CFG
            if _recap:
                _inject_budget = CTXGATE_INJECT_MAX_TOKENS_RECAP_CFG
            # Hard-clamp: never exceed ceiling - 500
            _ceiling = min(MAX_INPUT, MAX_CONTEXT - SAFETY_MARGIN - MAX_OUTPUT)
            _inject_budget = min(_inject_budget, _ceiling - 500)

            _block = ""
            if _ctx_parts:
                # Priority: 0=digest (newest, keep), 1=task memory, 2=knowledge (oldest, drop first)
                _prio_map = {"Relevant knowledge:": 2, "Task memory:": 1, "Session digest:": 0}
                _sections = []
                for part in _ctx_parts:
                    _prio = 1
                    for prefix, p in _prio_map.items():
                        if part.startswith(prefix):
                            _prio = p
                            break
                    _sections.append((_prio, part))
                _block = _fit_to_budget(_sections, _inject_budget)

            # Cache the block in window state
            if _ws_for_epoch is not None:
                _ws_for_epoch["injected_block"] = _block
                _ws_for_epoch["injected_epoch"] = _epoch_key

            log.debug("Epoch freeze: new injection block (%d chars) for %s (kn=%dch tm=%dch dig=%dch)",
                     len(_block), _epoch_key[:40], len(kn), len(tm), len(_digest))

        # Apply the block to the last user message
        if _block:
            last_user_idx = len(built) - 1
            for i in range(len(built) - 1, -1, -1):
                if built[i].get("role") == "user":
                    last_user_idx = i
                    break
            _raw_last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if _raw_last_user is not None and _msg_anchor(built[last_user_idx]) != _msg_anchor(_raw_last_user):
                log.error("Newest user message missing from built context (session=%s) - skipping memory injection", session_key)
                _app.metrics["inject_skipped_stale_user"] += 1
            else:
                existing = built[last_user_idx].get("content", "")
                if isinstance(existing, str):
                    built[last_user_idx]["content"] = existing + _block
                else:
                    built[last_user_idx]["content"] = list(existing) + [{"type": "text", "text": _block}]
                log.debug("Memory blocks appended to last user msg at pos %d (%dch)", last_user_idx, len(_block))

        # Instrumentation
        _record_injection(x_sid, tm if not isinstance(tm, Exception) else "", kn if not isinstance(kn, Exception) else "")
    else:
        # Old path (fallback)
        _t0 = time.monotonic()
        kn = ""  # knowledge injection disabled
        tm = await asyncio.gather(
            fetch_task_memory(x_sid, built, task_uuid=task_uuid, tenant_id=tenant_id),
            return_exceptions=True
        )
        _dt = (time.monotonic() - _t0) * 1000
        if _dt > 50:
            log.warning("SLOW: knowledge+memory fetch %.0fms", _dt)
        if isinstance(kn, Exception):
            log.warning("Knowledge injection failed: %s", kn)
            kn = ""
        if isinstance(tm, Exception):
            log.warning("Task memory injection failed: %s", tm)
            tm = ""
        if kn or tm:
            last_user_idx = len(built) - 1
            for i in range(len(built) - 1, -1, -1):
                if built[i].get("role") == "user":
                    last_user_idx = i
                    break
            _raw_last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if _raw_last_user is not None and _msg_anchor(built[last_user_idx]) != _msg_anchor(_raw_last_user):
                log.error("Newest user message missing from built context (session=%s) - skipping memory injection", session_key)
                _app.metrics["inject_skipped_stale_user"] += 1
                kn = tm = ""
        if kn or tm:
            _ctx_parts = []
            if kn:
                _ctx_parts.append("Relevant knowledge:" + chr(10) + kn)
            if tm:
                _ctx_parts.append("Task memory:" + chr(10) + tm)
            if _ctx_parts:
                _block = chr(10) + chr(10) + chr(10).join(_ctx_parts)
                existing = built[last_user_idx].get("content", "")
                if isinstance(existing, str):
                    built[last_user_idx]["content"] = existing + _block
                else:
                    built[last_user_idx]["content"] = list(existing) + [{"type": "text", "text": _block}]
                log.debug("Memory blocks appended to last user msg at pos %d (kn=%dch tm=%dch)", last_user_idx, len(kn), len(tm))
        _record_injection(x_sid, tm if not isinstance(tm, Exception) else "", kn if not isinstance(kn, Exception) else "")

    return built

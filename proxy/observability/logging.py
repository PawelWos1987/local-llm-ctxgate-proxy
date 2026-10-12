"""Logging filters and diagnostic helpers for ctxgate-proxy."""
from __future__ import annotations

import hashlib
import json
import logging

# TODO(I4): replace with AppContext
log = logging.getLogger("ctxgate-proxy")


class _AccessLogFilter(logging.Filter):
    """Drop uvicorn access-log records for the dashboard's
    high-frequency poll endpoints. Everything else passes."""
    _SILENT_PATHS = ("/health", "/v1/chat/completions")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            # uvicorn logs: (client_addr, method, path, http_version, status)
            if args and len(args) >= 3:
                method, path = args[1], args[2]
                if method in ("GET", "POST"):
                    for p in self._SILENT_PATHS:
                        if path == p or path.startswith(p + "?"):
                            return False
        except Exception:
            pass
        return True


def _prefix_diag(session_key: str, messages: list) -> None:
    """P8: compare per-message sha1 of the FINAL upstream messages against the
    previous request. Logs how many leading messages are byte-identical (the
    prefix that vLLM can KV-cache-reuse) and how many are new."""
    if not session_key:
        return

    # TODO(I4): replace with AppContext
    from proxy.app import session_prefix_hashes

    def _mh(m: dict) -> str:
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        tc = m.get("tool_calls")
        tc_s = json.dumps(tc, sort_keys=True) if tc else ""
        return hashlib.sha1((str(m.get("role", "")) + "|" + str(c) + "|" + tc_s).encode()).hexdigest()

    cur = [_mh(m) for m in messages]
    prev = session_prefix_hashes.get(session_key)
    if prev is not None:
        stable = 0
        for i in range(min(len(prev), len(cur))):
            if prev[i] == cur[i]:
                stable += 1
            else:
                break
        new_msgs = len(cur) - stable
        log.info("PREFIX session=%s stable_msgs=%d of %d new_msgs=%d", session_key, stable, len(cur), new_msgs)
    session_prefix_hashes[session_key] = cur

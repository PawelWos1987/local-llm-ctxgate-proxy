"""Per-session token tracking, call recording, and fd breakdown."""
import logging
import os
import time
from collections import deque

log = logging.getLogger("ctxgate-proxy")


def _track_session_tokens(session_key: str, input_tokens: int, output_tokens: int = 0, count_req: bool = True):
    """Update per-session token counters. count_req=False for output-only updates."""
    from proxy.app import SESSION_LAST_ACTIVE, session_tokens
    SESSION_LAST_ACTIVE[session_key] = time.time()
    if session_key not in session_tokens:
        session_tokens[session_key] = {"in": 0, "out": 0, "reqs": 0, "max_ctx": 0}
    st = session_tokens[session_key]
    st["in"] += input_tokens
    st["out"] += output_tokens
    if count_req:
        st["reqs"] += 1
    st["last_active"] = time.strftime("%H:%M:%S", time.localtime())
    st["max_ctx"] = max(st["max_ctx"], input_tokens)


def _record_call(session_key: str, input_tokens: int, output_tokens: int, status: str, model: str, stream: bool, detail: str = "", cached_tokens: int = 0):
    """Add to ring buffer of recent calls."""
    from proxy.app import recent_calls
    from proxy.utils.helpers import explain_status
    entry = {
        "ts": time.time(),
        "ts_human": time.strftime("%H:%M:%S", time.localtime()),
        "session": session_key,
        "in": input_tokens,
        "out": output_tokens,
        "cached": cached_tokens,
        "cached_pct": round(100 * cached_tokens / max(1, input_tokens), 1),
        "status": status,
        "model": model,
        "stream": stream,
        "detail": (detail or "")[:300],
        "explanation": explain_status(status, detail),
    }
    recent_calls.append(entry)


def _fd_breakdown() -> dict:
    """Classify this process's open file descriptors by type, plus TCP socket states."""
    by_type = {"socket": 0, "pipe": 0, "anon_inode": 0, "file": 0, "other": 0}
    total = 0
    try:
        for _fd in os.listdir("/proc/self/fd"):
            total += 1
            try:
                _t = os.readlink("/proc/self/fd/" + _fd)
            except OSError:
                by_type["other"] += 1
                continue
            if _t.startswith("socket:"):
                by_type["socket"] += 1
            elif _t.startswith("pipe:"):
                by_type["pipe"] += 1
            elif _t.startswith("anon_inode:"):
                by_type["anon_inode"] += 1
            elif _t.startswith("/") or _t.startswith("deleted"):
                by_type["file"] += 1
            else:
                by_type["other"] += 1
    except Exception:
        pass
    _tcp_names = {"01":"ESTABLISHED","06":"TIME_WAIT","08":"CLOSE_WAIT","0A":"LISTEN",
                  "07":"FIN_WAIT1","09":"CLOSING","04":"SYN_SENT","05":"SYN_RECV",
                  "02":"SYN_RECV","03":"FIN_WAIT2","0B":"LAST_ACK","0C":"LISTEN","0D":"CLOSED"}
    tcp_states = {}
    close_wait = 0
    for _p in ("/proc/self/net/tcp", "/proc/self/net/tcp6"):
        try:
            with open(_p) as _f:
                next(_f, None)
                for _line in _f:
                    _parts = _line.split()
                    if len(_parts) < 4:
                        continue
                    _st = _parts[3]
                    _nm = _tcp_names.get(_st, _st)
                    tcp_states[_nm] = tcp_states.get(_nm, 0) + 1
                    if _st == "08":
                        close_wait += 1
        except Exception:
            pass
    try:
        import resource
        _limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        _limit = -1
    return {"total": total, "limit": _limit, "by_type": by_type, "tcp_states": tcp_states, "close_wait": close_wait}


# ---------------------------------------------------------------------------
# Per-session exact prompt_tokens from vLLM (replaces estimator for reporting)
# ---------------------------------------------------------------------------
_session_exact_tokens: dict[str, int] = {}


def store_exact_tokens(session_key: str, prompt_tokens: int) -> None:
    """Store vLLM's exact prompt_tokens for a session (called after each response)."""
    if prompt_tokens > 0:
        _session_exact_tokens[session_key] = prompt_tokens


def get_exact_tokens(session_key: str) -> int | None:
    """Return stored exact prompt_tokens for a session, or None."""
    return _session_exact_tokens.get(session_key)


# ---------------------------------------------------------------------------
# Crash recovery: recover baseline from Goose's SQLite DB
# ---------------------------------------------------------------------------
import os as _os
import sqlite3 as _sqlite3

_GOOSE_DB_PATH = _os.environ.get(
    "CTXGATE_GOOSE_DB",
    "/home/pawelw/.local/share/goose/sessions/sessions.db"
)


def recover_session_baseline(session_key: str) -> int | None:
    """Recover the last known exact prompt_tokens from Goose's DB.

    Called lazily on first request for a session after proxy restart.
    The Goose DB stores session IDs without the 'goose:' prefix, so we
    strip it before querying.

    Returns the last input_tokens from usage_ledger, or None if not found.
    All DB access has a 2-second timeout and fails silently.
    """
    _goose_id = session_key
    if _goose_id.startswith("goose:"):
        _goose_id = _goose_id[len("goose:"):]
    try:
        _db = _sqlite3.connect(_GOOSE_DB_PATH, timeout=2)
        _cur = _db.execute(
            "SELECT input_tokens FROM usage_ledger WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (_goose_id,)
        )
        _row = _cur.fetchone()
        _db.close()
        if _row and _row[0] is not None:
            return int(_row[0])
    except Exception:
        pass
    return None


def prepopulate_session_baselines() -> int:
    """At app startup, scan Goose DB for recently-active sessions and
    populate _session_exact_tokens with their last known input_tokens.

    Returns the number of sessions recovered.
    All DB access has a 5-second timeout and fails silently.
    """
    if not os.path.exists(_GOOSE_DB_PATH):
        log.debug("Goose DB not found at %s, skipping prepopulation", _GOOSE_DB_PATH)
        return 0

    try:
        _db = _sqlite3.connect(_GOOSE_DB_PATH, timeout=5)
        # Get the most recent input_tokens for each session (top 100 most recent)
        _cur = _db.execute("""
            SELECT session_id, input_tokens
            FROM usage_ledger
            WHERE id IN (
                SELECT MAX(id) FROM usage_ledger GROUP BY session_id
            )
            ORDER BY created_timestamp DESC
            LIMIT 100
        """)
        _rows = _cur.fetchall()
        _db.close()

        _count = 0
        for _session_id, _input_tokens in _rows:
            if _input_tokens is not None:
                # Store with 'goose:' prefix to match session_key format
                _session_exact_tokens[f"goose:{_session_id}"] = int(_input_tokens)
                _count += 1

        if _count > 0:
            log.info("Prepopulated %d session baselines from Goose DB", _count)
        return _count

    except Exception as _e:
        log.warning("Failed to prepopulate session baselines: %s", _e)
        return 0

"""Worker status reading and stale session eviction."""
import logging
import os
import time

log = logging.getLogger("ctxgate-proxy")


def _read_worker_status() -> dict:
    """Read worker/.worker_status.json if it exists."""
    import json as _json
    path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "worker", ".worker_status.json"))
    try:
        with open(path, "r") as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        return {"alive": False, "lag_seconds": 0, "heartbeat": ""}


async def _worker_pending_count() -> int:
    """Return pending memory_jobs count, cached for WORKER_PENDING_TTL seconds."""
    from proxy.app import pool, WORKER_PENDING_CACHE, WORKER_PENDING_TTL
    now = time.time()
    if now - WORKER_PENDING_CACHE["ts"] < WORKER_PENDING_TTL:
        return WORKER_PENDING_CACHE["value"]
    try:
        if pool:
            v = await pool.fetchval(
                "SELECT COUNT(*) FROM proxy.memory_jobs WHERE status='pending'"
            )
            WORKER_PENDING_CACHE["value"] = int(v or 0)
            WORKER_PENDING_CACHE["ts"] = now
    except Exception as e:
        log.warning("worker_pending_count refresh failed: %s", e)
    return WORKER_PENDING_CACHE["value"]


async def _evict_stale_sessions():
    """Evict session state older than SESSION_TTL_HOURS."""
    from proxy.app import (
        SESSION_LAST_ACTIVE, session_seeds, session_compactions,
        session_tokens, session_prefix_hashes, _window_locks,
        _window_load_tried, _last_extract_anchor, _ensure_task_done,
        metrics, SESSION_TTL_HOURS,
    )
    from proxy.window.persist import _window_cleanup_ttl

    await _window_cleanup_ttl()
    now = time.time()
    ttl_sec = SESSION_TTL_HOURS * 3600
    stale = [k for k, ts in SESSION_LAST_ACTIVE.items() if now - ts > ttl_sec]
    for k in stale:
        _last_extract_anchor.pop(k, None)
        _ensure_task_done.discard(k)
        session_seeds.pop(k, None)
        session_compactions.pop(k, None)
        session_tokens.pop(k, None)
        SESSION_LAST_ACTIVE.pop(k, None)
        session_prefix_hashes.pop(k, None)
        _window_locks.pop(k, None)
        _window_load_tried.discard(k)
    if stale:
        metrics["evicted_sessions"] += len(stale)
        log.info("Evicted %d stale sessions (TTL %dh)", len(stale), SESSION_TTL_HOURS)

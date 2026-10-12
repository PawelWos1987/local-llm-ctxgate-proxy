"""Admin API endpoints: metrics, sessions, recent calls, errors, memory summary."""
import time
import logging

from fastapi import Request

from proxy.lifecycle.injection import _human_time
from proxy.utils.worker import _read_worker_status, _worker_pending_count

log = logging.getLogger("ctxgate-proxy")


async def api_metrics(request: Request):
    """Structured metrics for dashboard (JSON)."""
    import proxy.app as _app
    _worker_pending = await _worker_pending_count()
    uptime = time.time() - _app.metrics["started_at"]
    _hit_rate = _app.metrics["cached_tokens_total"] / max(1, _app.metrics["prompt_tokens_total"])
    _eff_prefill = (_app.metrics["prompt_tokens_total"] - _app.metrics["cached_tokens_total"]) / max(1, _app.metrics["requests_ok"])
    return {
        **_app.metrics,
        "cache_hit_rate": round(_hit_rate, 4),
        "effective_prefill_per_request": round(_eff_prefill, 1),
        "uptime_sec": round(uptime, 1),
        "uptime_human": _human_time(uptime),
        "started_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_app.metrics["started_at"])),
        "active_sessions": len(_app.session_tokens),
        "worker": _read_worker_status(),
        "worker_pending": _worker_pending,
    }


async def api_sessions(request: Request):
    """Per-session token accounting."""
    import proxy.app as _app
    sessions = []
    for key, st in _app.session_tokens.items():
        fp = ""
        sessions.append({
            "key": key,
            "provider": key.split(":")[0] if ":" in key else key,
            "fingerprint": fp,
            "tokens_in": st["in"],
            "tokens_out": st["out"],
            "requests": st["reqs"],
            "max_context": st["max_ctx"],
            "last_active": st.get("last_active", ""),
        })
    sessions.sort(key=lambda s: s["requests"], reverse=True)
    return sessions


async def api_recent_calls(request: Request, n: int = 50):
    """Recent call ring buffer."""
    import proxy.app as _app
    items = list(_app.recent_calls)
    return list(reversed(items[-n:]))


async def api_errors(request: Request, n: int = 20):
    """Recent errors from ring buffer."""
    import proxy.app as _app
    errs = [c for c in _app.recent_calls if c["status"] != "ok"]
    return list(reversed(errs[-n:]))


async def api_memory_summary(request: Request):
    """Summary of tasks and working memory from DB."""
    import proxy.app as _app
    if not _app.pool:
        return {"tasks": [], "memory_entries": 0}
    try:
        tasks = await _app.pool.fetch("SELECT session_id, created_at, updated_at FROM proxy.tasks ORDER BY updated_at DESC LIMIT 50")
        mem_count = await _app.pool.fetchval("SELECT COUNT(*) FROM proxy.working_memory")
        result = {
            "tasks": [
                {
                    "session_id": t["session_id"],
                    "created": str(t["created_at"]),
                    "updated": str(t["updated_at"]),
                }
                for t in tasks
            ],
            "memory_entries": mem_count,
        }
        return result
    except Exception as e:
        log.warning("memory-summary query failed: %s", e)
        return {"tasks": [], "memory_entries": 0, "error": str(e)}

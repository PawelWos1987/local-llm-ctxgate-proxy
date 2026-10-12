"""Health, readiness, and Prometheus metrics endpoints."""
import os
import time
import logging

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from proxy.config import VLLM_URL
from proxy.token.calibrator import calibrator
from proxy.lifecycle.injection import _human_time
from proxy.utils.worker import _read_worker_status, _worker_pending_count
from proxy.hotpath.tracking import _fd_breakdown

log = logging.getLogger("ctxgate-proxy")


async def health():
    import proxy.app as _app
    try:
        _fd_count = len(os.listdir("/proc/self/fd"))
    except Exception:
        _fd_count = -1
    return {"status": "ok", "version": "1.0.0", "sessions": len(_app.session_tokens), "fd_count": _fd_count, "fd": _fd_breakdown()}


async def get_metrics(request: Request):
    import proxy.app as _app
    uptime = time.time() - _app.metrics["started_at"]
    result = dict(_app.metrics)
    result["uptime_human"] = _human_time(uptime)
    result["started_human"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_app.metrics["started_at"]))
    result["lm_rate_limiter"] = _app._lm_rate_limiter.stats if _app._lm_rate_limiter else None
    result["active_sessions"] = len(_app.session_tokens)
    result["recent_calls_count"] = len(_app.recent_calls)
    return result


async def ready():
    import proxy.app as _app
    checks = {}
    ok = True
    try:
        if _app.pool:
            await _app.pool.fetchval("SELECT 1")
            checks["postgres"] = "ok"
        else:
            checks["postgres"] = "no pool"
            ok = False
    except Exception as e:
        checks["postgres"] = "error: " + str(e)[:80]
        ok = False
    if _app.enc:
        checks["tokenizer"] = "ok"
    else:
        checks["tokenizer"] = "missing"
        ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(VLLM_URL + "/models")
            checks["vllm"] = "ok" if r.status_code == 200 else "http_" + str(r.status_code)
            if r.status_code != 200:
                ok = False
    except Exception:
        checks["vllm"] = "unreachable"
        ok = False
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


async def metrics_prometheus(request: Request):
    import proxy.app as _app
    uptime = time.time() - _app.metrics["started_at"]
    _worker_pending = await _worker_pending_count()
    lines = [
        "# TYPE ctxgate_requests_total counter", "ctxgate_requests_total %d" % _app.metrics["requests_total"],
        "# TYPE ctxgate_requests_ok counter", "ctxgate_requests_ok %d" % _app.metrics["requests_ok"],
        "# TYPE ctxgate_requests_error counter", "ctxgate_requests_error %d" % _app.metrics["requests_error"],
        "# TYPE ctxgate_cached_tokens_total counter", "ctxgate_cached_tokens_total %d" % _app.metrics["cached_tokens_total"],
        "# TYPE ctxgate_prompt_tokens_total counter", "ctxgate_prompt_tokens_total %d" % _app.metrics["prompt_tokens_total"],
        "# TYPE ctxgate_tool_call_in_reasoning counter", "ctxgate_tool_call_in_reasoning %d" % _app.metrics["tool_call_in_reasoning"],
        "# TYPE ctxgate_empty_after_retry counter", "ctxgate_empty_after_retry %d" % _app.metrics["empty_after_retry"],
        "# TYPE ctxgate_tool_args_unicode_escape counter", "ctxgate_tool_args_unicode_escape %d" % _app.metrics["tool_args_unicode_escape"],
        "# TYPE ctxgate_cache_hit_rate gauge", "ctxgate_cache_hit_rate %.4f" % (_app.metrics["cached_tokens_total"] / max(1, _app.metrics["prompt_tokens_total"])),
        "# TYPE ctxgate_effective_prefill_per_request gauge", "ctxgate_effective_prefill_per_request %.1f" % ((_app.metrics["prompt_tokens_total"] - _app.metrics["cached_tokens_total"]) / max(1, _app.metrics["requests_ok"])),
        "# TYPE ctxgate_evicted_sessions_total counter", "ctxgate_evicted_sessions_total %d" % _app.metrics["evicted_sessions"],
        "# TYPE ctxgate_trim_events counter", "ctxgate_trim_events %d" % _app.metrics["trim_events"],
        "# TYPE ctxgate_window_collapse_guard counter", "ctxgate_window_collapse_guard %d" % _app.metrics["window_collapse_guard"],
        "# TYPE ctxgate_trim_sticky_reuse counter", "ctxgate_trim_sticky_reuse %d" % _app.metrics["trim_sticky_reuse"],
        "# TYPE ctxgate_emergency_shrink_total counter", "ctxgate_emergency_shrink_total %d" % _app.metrics["emergency_shrink_total"],
        "# TYPE ctxgate_emergency_shrink_groups_dropped counter", "ctxgate_emergency_shrink_groups_dropped %d" % _app.metrics["emergency_shrink_groups_dropped"],
        "# TYPE ctxgate_dropped_total counter", "ctxgate_dropped_total %d" % _app.metrics["dropped_total"],
        "# TYPE ctxgate_summary_slices_ok counter", "ctxgate_summary_slices_ok %d" % _app.metrics["summary_slices_ok"],
        "# TYPE ctxgate_summary_slices_failed counter", "ctxgate_summary_slices_failed %d" % _app.metrics["summary_slices_failed"],
        "# TYPE ctxgate_window_loads_db counter", "ctxgate_window_loads_db %d" % _app.metrics["window_loads_db"],
        "# TYPE ctxgate_window_persist_errors counter", "ctxgate_window_persist_errors %d" % _app.metrics["window_persist_errors"],
        "# TYPE ctxgate_toolcall_strips counter", "ctxgate_toolcall_strips %d" % _app.metrics["toolcall_strips"],
        "# TYPE ctxgate_tokens_in_total counter", "ctxgate_tokens_in_total %d" % _app.metrics["tokens_in_total"],
        "# TYPE ctxgate_tokens_out_total counter", "ctxgate_tokens_out_total %d" % _app.metrics["tokens_out_total"],
        "# TYPE ctxgate_max_context_seen gauge", "ctxgate_max_context_seen %d" % _app.metrics["max_context_seen"],
        "# TYPE ctxgate_active_sessions gauge", "ctxgate_active_sessions %d" % len(_app.session_tokens),
        "# TYPE ctxgate_uptime_seconds gauge", "ctxgate_uptime_seconds %.1f" % uptime,
        "# TYPE ctxgate_extract_queue_depth gauge", "ctxgate_extract_queue_depth %d" % _app._EXTRACT_IN_FLIGHT,
        "# TYPE ctxgate_worker_lag_seconds gauge", "ctxgate_worker_lag_seconds %.1f" % _read_worker_status().get("lag_seconds", 0),
        "# TYPE ctxgate_worker_pending gauge", "ctxgate_worker_pending %d" % _worker_pending,
        "# TYPE ctxgate_estimator_under_total counter", "ctxgate_estimator_under_total %d" % calibrator.under_total,
        "# TYPE ctxgate_estimator_over_total counter", "ctxgate_estimator_over_total %d" % calibrator.over_total,
        "# TYPE ctxgate_estimator_max_under_pct gauge", "ctxgate_estimator_max_under_pct %.4f" % calibrator.max_under_pct,
        "# TYPE ctxgate_estimator_max_over_pct gauge", "ctxgate_estimator_max_over_pct %.4f" % calibrator.max_over_pct,
        "# TYPE ctxgate_estimator_ratio gauge", "ctxgate_estimator_ratio %.4f" % calibrator.ratio,
    ]
    lines.extend([
        "# TYPE ctxgate_lm_tokens_total counter",
        *(('ctxgate_lm_tokens_total{kind="%s",direction="%s"} %d' % (k, d, v))
          for k in ("knowledge", "phase", "root", "other")
          for d, v in _app.metrics["lm_tokens_by_kind"].get(k, {}).items()),
        "# TYPE ctxgate_lm_calls_total counter",
        *((('ctxgate_lm_calls_total{kind="%s"} %d' % (k, v))
          for k, v in _app.metrics["lm_calls_by_kind"].items())),
    ])
    return Response("\n".join(lines) + "\n", media_type="text/plain")

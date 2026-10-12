"""Injection metrics: track memory/knowledge injection utilization."""
import json
import logging
import os
import time

log = logging.getLogger("ctxgate-proxy")

INJECTION_METRICS_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "injection_metrics.json"))

def _default_injection_metrics() -> dict:
    return {
        "task_memory_injections": 0,
        "task_memory_tokens": 0,
        "knowledge_injections": 0,
        "knowledge_tokens": 0,
        "working_memory_injections": 0,
        "working_memory_tokens": 0,
        "total_requests": 0,
        "sessions": {},
        "last_injected_task_memory": "",
        "last_injected_knowledge": "",
        "events": [],
    }

def _load_injection_metrics() -> dict:
    try:
        with open(INJECTION_METRICS_PATH) as f:
            data = json.load(f)
        base = _default_injection_metrics()
        if isinstance(data, dict):
            base.update(data)
        return base
    except Exception:
        return _default_injection_metrics()

_last_metrics_save = 0.0

def _save_injection_metrics() -> None:
    global _last_metrics_save
    if time.monotonic() - _last_metrics_save < 5.0: return
    _last_metrics_save = time.monotonic()
    try:
        from proxy.app import injection_metrics
        with open(INJECTION_METRICS_PATH, "w") as f:
            json.dump(injection_metrics, f)
    except Exception as e:
        log.warning("injection metrics save failed: %s", e)

async def _save_injection_metrics_async() -> None:
    import asyncio
    global _last_metrics_save
    if time.monotonic() - _last_metrics_save < 5.0:
        return
    _last_metrics_save = time.monotonic()
    from proxy.app import injection_metrics
    snapshot = dict(injection_metrics)
    def _write():
        with open(INJECTION_METRICS_PATH, "w") as f:
            json.dump(snapshot, f)
    try:
        await asyncio.get_event_loop().run_in_executor(None, _write)
    except Exception as e:
        log.warning("injection metrics save failed: %s", e)

def _pct_str(n: int, d: int) -> str:
    return f"{(100 * n / d):.1f}%" if d else "0.0%"

def _record_injection(session_id: str, tm: str, kn: str) -> None:
    """Record memory/knowledge injection utilization. Lightweight, no DB round-trips."""
    global injection_metrics
    try:
        from proxy.app import injection_metrics, _spawn, count_tokens
        injection_metrics["total_requests"] += 1
        events = []
        if tm:
            t = count_tokens(tm)
            injection_metrics["task_memory_injections"] += 1
            injection_metrics["task_memory_tokens"] += t
            injection_metrics["last_injected_task_memory"] = tm[:2000]
            events.append({"ts": time.time(), "session": session_id, "type": "task_memory", "tokens": t})
        if kn:
            t = count_tokens(kn)
            injection_metrics["knowledge_injections"] += 1
            injection_metrics["knowledge_tokens"] += t
            injection_metrics["last_injected_knowledge"] = kn[:2000]
            events.append({"ts": time.time(), "session": session_id, "type": "knowledge", "tokens": t})
        # Working memory is a subset of task memory; count it when the WM line is present.
        wm_line = ""
        if tm and "WORKING MEMORY:" in tm:
            for ln in tm.split("\n"):
                if ln.startswith("WORKING MEMORY:"):
                    wm_line = ln
                    break
        if wm_line:
            injection_metrics["working_memory_injections"] += 1
            injection_metrics["working_memory_tokens"] += count_tokens(wm_line)
        s = injection_metrics["sessions"].setdefault(session_id, {"task_mem": 0, "knowledge": 0, "wm": 0, "requests": 0})
        s["requests"] += 1
        if tm:
            s["task_mem"] += 1
        if kn:
            s["knowledge"] += 1
        if wm_line:
            s["wm"] += 1
        for e in events:
            injection_metrics["events"].append(e)
        if len(injection_metrics["events"]) > 50:
            injection_metrics["events"] = injection_metrics["events"][-50:]
        _spawn(_save_injection_metrics_async())
    except Exception as e:
        log.warning("injection metrics record failed: %s", e)

def _human_time(seconds: float) -> str:
    """Convert seconds to human readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        m = int(seconds // 60)
        s = seconds % 60
        return f"{m}m {s:.0f}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"

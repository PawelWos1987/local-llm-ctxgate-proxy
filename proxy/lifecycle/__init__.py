"""Lifecycle package: lifespan, recovery, watchdog, injection metrics."""
from proxy.lifecycle.injection import (
    _default_injection_metrics, _load_injection_metrics,
    _save_injection_metrics, _save_injection_metrics_async,
    _record_injection, _pct_str, _human_time,
)
from proxy.lifecycle.recovery import _spawn, _restart_recovery
from proxy.lifecycle.watchdog import _sd_notify, _watchdog_loop, _memory_worker_loop, _fd_hygiene_loop
from proxy.lifecycle.lifespan import lifespan

__all__ = [
    "_default_injection_metrics", "_load_injection_metrics",
    "_save_injection_metrics", "_save_injection_metrics_async",
    "_record_injection", "_pct_str", "_human_time",
    "_spawn", "_restart_recovery",
    "_sd_notify", "_watchdog_loop", "_memory_worker_loop", "_fd_hygiene_loop",
    "lifespan",
]

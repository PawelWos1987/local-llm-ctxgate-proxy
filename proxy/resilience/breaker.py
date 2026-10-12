"""Circuit breaker for backend connections (vLLM and LM).

Opens after N consecutive failures, half-open probes after T seconds.
When open, callers get an immediate _BreakerOpen exception (fail-fast).
"""
from __future__ import annotations

import logging
import time

# TODO(I4): replace with AppContext
log = logging.getLogger("ctxgate-proxy")


class _CircuitBreaker:
    """Opens after N consecutive failures, half-open probes after T seconds.
    When open, callers get an immediate _BreakerOpen exception (fail-fast)."""
    def __init__(self, name: str, threshold: int = 5, cooldown: float = 30.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._open_at = 0.0  # timestamp when breaker opened

    @property
    def is_open(self) -> bool:
        if self._failures < self.threshold:
            return False
        import time as _t
        return (_t.monotonic() - self._open_at) < self.cooldown

    @property
    def is_half_open(self) -> bool:
        if self._failures < self.threshold:
            return False
        import time as _t
        return (_t.monotonic() - self._open_at) >= self.cooldown

    def record_success(self) -> None:
        self._failures = 0
        self._open_at = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            import time as _t
            self._open_at = _t.monotonic()
            log.warning("CircuitBreaker[%s] OPEN after %d consecutive failures (cooldown %.0fs)",
                        self.name, self._failures, self.cooldown)

    def allow(self) -> bool:
        """True if the request should proceed (closed or half-open)."""
        if self._failures < self.threshold:
            return True
        import time as _t
        return (_t.monotonic() - self._open_at) >= self.cooldown


class _BreakerOpen(Exception):
    """Raised when a circuit breaker is open (fail-fast)."""
    pass


_vllm_breaker = _CircuitBreaker("vllm", threshold=5, cooldown=30.0)
_lm_breaker = _CircuitBreaker("lm", threshold=5, cooldown=30.0)


def _check_vllm_breaker() -> None:
    """Raise _BreakerOpen if the vLLM breaker is open."""
    if not _vllm_breaker.allow():
        raise _BreakerOpen(f"vLLM breaker open ({_vllm_breaker._failures} consecutive failures, cooldown {_vllm_breaker.cooldown}s)")


def _check_lm_breaker() -> None:
    """Raise _BreakerOpen if the LM breaker is open."""
    if not _lm_breaker.allow():
        raise _BreakerOpen(f"LM breaker open ({_lm_breaker._failures} consecutive failures, cooldown {_lm_breaker.cooldown}s)")

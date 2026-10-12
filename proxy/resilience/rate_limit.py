"""Token bucket rate limiter and per-tenant concurrency control."""
from __future__ import annotations

import asyncio
import time

from proxy.config.constants import (
    CTXGATE_TENANT_RATE_PER_SEC,
    CTXGATE_TENANT_RATE_BURST,
    CTXGATE_TENANT_MAX_CONCURRENCY,
)


class _TokenBucket:
    """Classic token bucket rate limiter. Single event loop = no locking needed."""

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now

    async def acquire(self, amount: int = 1) -> None:
        if self.rate <= 0:
            return  # Rate limiting disabled
        while True:
            self._refill()
            if self.tokens >= amount:
                self.tokens -= amount
                return
            deficit = amount - self.tokens
            await asyncio.sleep(deficit / self.rate)


# Per-tenant rate limiters and semaphores (created on demand)
# TODO(I4): replace with AppContext
_tenant_rate_limiters: dict[str, _TokenBucket] = {}
_tenant_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_tenant_rate_limiter(tenant_id: str) -> _TokenBucket:
    if tenant_id not in _tenant_rate_limiters:
        rate = CTXGATE_TENANT_RATE_PER_SEC
        burst = CTXGATE_TENANT_RATE_BURST if CTXGATE_TENANT_RATE_BURST > 0 else max(1, int(rate))
        _tenant_rate_limiters[tenant_id] = _TokenBucket(rate, burst)
    return _tenant_rate_limiters[tenant_id]


def _get_tenant_semaphore(tenant_id: str) -> asyncio.Semaphore:
    if tenant_id not in _tenant_semaphores:
        _tenant_semaphores[tenant_id] = asyncio.Semaphore(CTXGATE_TENANT_MAX_CONCURRENCY)
    return _tenant_semaphores[tenant_id]

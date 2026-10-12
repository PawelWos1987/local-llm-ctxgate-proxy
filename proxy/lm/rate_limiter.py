"""Mistral API rate limiter: RPS, TPM, and concurrency control."""
import asyncio
import logging
import os
import time

from proxy.resilience.rate_limit import _TokenBucket

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


class MistralRateLimiter:
    """Enforces Mistral API limits: 500K TPM, 16.67 RPS.

    Single shared instance. All workers call acquire() before the HTTP
    request and release() after. The only component that knows the limits.
    """

    RPS_LIMIT = 16.67
    RPS_BURST = 16
    TPM_LIMIT = 500_000
    TPM_RATE = TPM_LIMIT / 60.0
    TPM_BURST = 500_000
    MAX_CONCURRENT = int(os.environ.get("CTXGATE_LM_MAX_CONCURRENT", "10"))

    def __init__(self):
        self._rps = _TokenBucket(self.RPS_LIMIT, self.RPS_BURST)
        self._tpm = _TokenBucket(self.TPM_RATE, self.TPM_BURST)
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._active = 0
        self._total_acquired = 0
        self._total_waited = 0.0

    async def acquire(self, estimated_tokens: int = 2000) -> float:
        t0 = time.monotonic()
        await self._rps.acquire(1)
        await self._tpm.acquire(estimated_tokens)
        await self._sem.acquire()
        self._active += 1
        self._total_acquired += 1
        wait = time.monotonic() - t0
        self._total_waited += wait
        if wait > 2.0:
            log.warning("RateLimiter: waited %.1fs for slot (active=%d, rps_avail=%.1f, tpm_avail=%d)",
                        wait, self._active, self._rps.tokens,
                        int(self._tpm.burst - self._tpm.tokens))
        return wait

    def release(self, actual_tokens: int = 0, estimated_tokens: int = 2000):
        self._active -= 1
        self._sem.release()
        if actual_tokens and actual_tokens < estimated_tokens:
            self._tpm.tokens = min(self._tpm.burst, self._tpm.tokens + (estimated_tokens - actual_tokens))

    @property
    def stats(self) -> dict:
        return {
            "active": self._active,
            "rps_available": round(self._rps.tokens, 1),
            "tpm_available": int(self._tpm.tokens),
            "total_acquired": self._total_acquired,
            "avg_wait_s": round(self._total_waited / max(1, self._total_acquired), 3),
        }

# Single global instance
_lm_rate_limiter = MistralRateLimiter()

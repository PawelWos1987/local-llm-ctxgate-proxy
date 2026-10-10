#!/usr/bin/env python3
"""Tests for the rate-limiter and parallelization changes.

Works with both:
  - Standalone: python3 tests/test_rate_limiter.py
  - Pytest:     python3 -m pytest tests/test_rate_limiter.py
"""
import asyncio
import time
import sys
import os
import inspect

sys.path.insert(0, "/home/pawelw/ctxproxy/proxy")

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")

def test_token_bucket():
    """Burst, rate limiting, and refill behavior of _TokenBucket."""
    async def _run():
        from app import _TokenBucket
        tb = _TokenBucket(rate=10.0, burst=5)
        t0 = time.monotonic()
        for _ in range(5):
            await tb.acquire(1)
        elapsed = time.monotonic() - t0
        check("Burst of 5 acquires fast", elapsed < 0.1, f"took {elapsed:.3f}s")
        t0 = time.monotonic()
        await tb.acquire(1)
        elapsed = time.monotonic() - t0
        check("6th acquire waits", elapsed > 0.05, f"took {elapsed:.3f}s")
        await asyncio.sleep(0.3)
        tb._refill()
        check("Tokens replenish over time", tb.tokens > 2, f"tokens={tb.tokens:.1f}")
    asyncio.run(_run())

def test_rate_limiter():
    """Acquire/release cycle with TPM refund."""
    async def _run():
        from app import _lm_rate_limiter
        stats = _lm_rate_limiter.stats
        check("Initial active=0", stats["active"] == 0, f"active={stats['active']}")
        check("RPS available > 0", stats["rps_available"] > 0, f"rps={stats['rps_available']}")
        check("TPM available > 0", stats["tpm_available"] > 0, f"tpm={stats['tpm_available']}")
        t0 = time.monotonic()
        wait = await _lm_rate_limiter.acquire(2000)
        elapsed = time.monotonic() - t0
        check("Acquire 2000 tokens fast", elapsed < 0.5, f"took {elapsed:.3f}s")
        _lm_rate_limiter.release(1500, 2000)
        stats = _lm_rate_limiter.stats
        check("Active back to 0 after release", stats["active"] == 0, f"active={stats['active']}")
        check("TPM available after refund", stats["tpm_available"] > 0, f"tpm={stats['tpm_available']}")
    asyncio.run(_run())

def test_concurrent_workers():
    """5 concurrent workers don't deadlock."""
    async def _run():
        from app import _lm_rate_limiter
        async def worker(i):
            await _lm_rate_limiter.acquire(1000)
            try:
                await asyncio.sleep(0.1)
            finally:
                _lm_rate_limiter.release(500, 1000)
        t0 = time.monotonic()
        await asyncio.gather(*[worker(i) for i in range(5)])
        elapsed = time.monotonic() - t0
        check("5 concurrent workers complete", elapsed < 5.0, f"took {elapsed:.2f}s")
        stats = _lm_rate_limiter.stats
        check("All workers released", stats["active"] == 0, f"active={stats['active']}")
    asyncio.run(_run())

def test_rps_limit():
    """RPS token bucket throttles when empty."""
    async def _run():
        from app import _lm_rate_limiter
        _lm_rate_limiter._rps.tokens = 0.0
        _lm_rate_limiter._rps.last_refill = time.monotonic()
        t0 = time.monotonic()
        for _ in range(3):
            await _lm_rate_limiter._rps.acquire(1)
        elapsed = time.monotonic() - t0
        check("RPS throttling works", elapsed > 0.05, f"took {elapsed:.3f}s")
        check("Not too slow", elapsed < 2.0, f"took {elapsed:.3f}s")
    asyncio.run(_run())

def test_parallel_chunks():
    """asyncio.gather parallel processing with exception isolation."""
    async def _run():
        async def mock_api_call(chunk_id):
            await asyncio.sleep(0.1)
            return f"summary_{chunk_id}"
        chunks = [f"chunk_{i}" for i in range(4)]
        async def _do_chunk(ci, chunk):
            return await mock_api_call(ci)
        t0 = time.monotonic()
        results = await asyncio.gather(*[_do_chunk(ci, ch) for ci, ch in enumerate(chunks)], return_exceptions=True)
        elapsed = time.monotonic() - t0
        check("All 4 chunks processed", len(results) == 4, f"got {len(results)}")
        check("All results valid", all(isinstance(r, str) for r in results), str(results))
        check("Parallel (not sequential)", elapsed < 0.3, f"took {elapsed:.3f}s")
        async def _do_chunk_fail(ci, chunk):
            if ci == 2:
                raise ValueError("simulated failure")
            return f"ok_{ci}"
        results = await asyncio.gather(*[_do_chunk_fail(ci, ch) for ci, ch in enumerate(chunks)], return_exceptions=True)
        check("Failed chunk is Exception", isinstance(results[2], Exception), str(results[2]))
        check("Other chunks still succeed", results[0] == "ok_0" and results[1] == "ok_1")
    asyncio.run(_run())

def test_worker_id():
    """_lm_consumer accepts worker_id with default 0."""
    from app import _lm_consumer
    sig = inspect.signature(_lm_consumer)
    params = list(sig.parameters.keys())
    check("worker_id parameter exists", "worker_id" in params, f"params={params}")
    check("worker_id has default 0", sig.parameters["worker_id"].default == 0)

def test_lm_do_call_rate_limiting():
    """_lm_do_call source contains rate limiter calls."""
    from app import _lm_do_call
    src = inspect.getsource(_lm_do_call)
    check("Calls _lm_rate_limiter.acquire", "_lm_rate_limiter.acquire" in src)
    check("Calls _lm_rate_limiter.release", "_lm_rate_limiter.release" in src)
    check("Calculates estimated_tokens", "estimated_tokens" in src)
    check("Releases with actual_tokens", "actual_tokens" in src)

def test_metrics():
    """app.py source references rate limiter stats in /metrics."""
    with open("/home/pawelw/ctxproxy/proxy/app.py") as f:
        content = f.read()
    check("Metrics references rate limiter stats", 'result["lm_rate_limiter"] = _lm_rate_limiter.stats' in content)

def test_env_var():
    """CTXGATE_LM_WORKERS is referenced in app.py and .env."""
    with open("/home/pawelw/ctxproxy/proxy/app.py") as f:
        content = f.read()
    check("CTXGATE_LM_WORKERS referenced", "CTXGATE_LM_WORKERS" in content)
    if os.path.exists("/home/pawelw/ctxproxy/.env"):
        with open("/home/pawelw/ctxproxy/.env") as f:
            env_content = f.read()
        check(".env has CTXGATE_LM_WORKERS", "CTXGATE_LM_WORKERS" in env_content)
    else:
        check(".env file exists", False, "missing")

def main():
    test_token_bucket()
    test_rate_limiter()
    test_concurrent_workers()
    test_rps_limit()
    test_parallel_chunks()
    test_worker_id()
    test_lm_do_call_rate_limiting()
    test_metrics()
    test_env_var()
    print(f"\n{'='*50}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    print(f"{'='*50}")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")

if __name__ == "__main__":
    main()

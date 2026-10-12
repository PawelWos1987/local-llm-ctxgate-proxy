"""B13: window persistence must not be disabled forever after one transient error.

Current behavior: _window_persist sets a 60s monotonic cooldown on a transient
PG error and retries after. A failure must never fail a request.

P4: The DB write is now a background asyncio task. Tests must flush pending
persists to observe the actual DB interaction.
"""

import asyncio
import os
import sys
import contextlib
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "

import proxy.app as app  # noqa: E402
from proxy.window.persist import (
    _window_persist, _flush_pending_persists,
    _window_persist_enabled, _window_persist_warned,
    _window_persist_cooldown_until,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _MockConn:
    """Mock asyncpg connection that delegates to the pool's execute."""
    def __init__(self, pool):
        self._pool = pool

    async def execute(self, *a, **k):
        return await self._pool.execute(*a, **k)

    async def fetchrow(self, *a, **k):
        return None


class _MockPool:
    """Mock pool with acquire() context manager that delegates to execute."""
    def __init__(self, fail_mode="none"):
        self.calls = 0
        self.fail_mode = fail_mode  # "none", "once", "always"

    async def execute(self, *a, **k):
        self.calls += 1
        if self.fail_mode == "once" and self.calls == 1:
            raise Exception("transient pg error")
        if self.fail_mode == "always":
            raise Exception("boom")
        return None

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _MockConn(self)


def _reset_state():
    import proxy.window.persist as persist_mod
    persist_mod._window_persist_enabled = None
    persist_mod._window_persist_warned = False
    persist_mod._window_persist_cooldown_until = 0.0
    persist_mod._pending_persists.clear()
    persist_mod._persist_failure_count = 0
    persist_mod.clear_window_cache()


def test_b13_transient_error_then_recovers():
    """First persist call fails (transient), second call (after cooldown) writes."""
    _reset_state()
    pool = _MockPool(fail_mode="once")
    ws = {"cut": 3, "cut_anchor": "a", "cut_prev_anchor": "p",
          "seed_sig": "s", "summarized_through": 2, "dropped_total": 1}

    async def _scenario():
        import proxy.window.persist as persist_mod
        with patch.object(app, "pool", pool):
            # 1st call: schedules background task that will fail (transient)
            await _window_persist("sess-b13", dict(ws))
            # Flush to execute the background task
            await _flush_pending_persists()
            assert pool.calls == 1, "first call should have attempted"

            # Simulate 60s passing by advancing the monotonic clock past the cooldown.
            persist_mod._window_persist_cooldown_until = 0.0  # force cooldown to be over

            # 2nd call: should now write (calls becomes 2) under the fix.
            await _window_persist("sess-b13", dict(ws))
            await _flush_pending_persists()

    _run(_scenario())

    assert pool.calls == 2, (
        f"expected 2 write attempts (recover after cooldown), got {pool.calls}. "
        f"persistence is permanently disabled after one transient error."
    )


def test_b13_never_fails_request():
    """_window_persist must swallow the error and return None (never raise)."""
    _reset_state()

    pool = _MockPool(fail_mode="always")
    ws = {"cut": 3, "cut_anchor": "a", "cut_prev_anchor": "p",
          "seed_sig": "s", "summarized_through": 2, "dropped_total": 1}

    async def _scenario():
        import proxy.window.persist as persist_mod
        with patch.object(app, "pool", pool):
            result = await _window_persist("sess-b13", dict(ws))
            # Flush to execute the background task (will fail, but must not propagate)
            await _flush_pending_persists()
            return result

    result = _run(_scenario())
    assert result is None, "persist must return None, never raise"

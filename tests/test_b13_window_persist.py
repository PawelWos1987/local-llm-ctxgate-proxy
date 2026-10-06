"""B13: window persistence must not be disabled forever after one transient error.

_current behavior: _window_persist sets _window_persist_enabled=False on any
exception and never re-enables it, so one transient PG error means no window
state is saved until process restart. The fix replaces the permanent disable
with a 60s monotonic cooldown: skip writes during the cooldown, retry after.
A failure must never fail a request.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "

import proxy.app as app  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FailOncePool:
    """pool.execute raises the first call, succeeds after. Records call count."""
    def __init__(self):
        self.calls = 0

    async def execute(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            raise Exception("transient pg error")
        return None


def _reset_state():
    app._window_persist_enabled = None
    app._window_persist_warned = False
    if hasattr(app, "_window_persist_cooldown_until"):
        app._window_persist_cooldown_until = 0.0


def test_b13_transient_error_then_recovers():
    """First persist call fails (transient), second call (after cooldown) writes."""
    _reset_state()
    pool = _FailOncePool()
    ws = {"cut": 3, "cut_anchor": "a", "cut_prev_anchor": "p",
          "seed_sig": "s", "summarized_through": 2, "dropped_total": 1}

    with patch.object(app, "pool", pool):
        # 1st call: fails (transient). Must NOT raise, must not fail a request.
        _run(app._window_persist("sess-b13", dict(ws)))
        assert pool.calls == 1, "first call should have attempted"

        # Simulate 60s passing by advancing the monotonic clock past the cooldown.
        if hasattr(app, "_window_persist_cooldown_until"):
            app._window_persist_cooldown_until = 0.0  # force cooldown to be over
        else:
            # OLD behavior: flag is permanently False -> second call is a no-op.
            pass

        # 2nd call: should now write (calls becomes 2) under the fix.
        _run(app._window_persist("sess-b13", dict(ws)))

    assert pool.calls == 2, (
        f"expected 2 write attempts (recover after cooldown), got {pool.calls}. "
        f"persistence is permanently disabled after one transient error."
    )


def test_b13_never_fails_request():
    """_window_persist must swallow the error and return None (never raise)."""
    _reset_state()

    class _AlwaysFailPool:
        def __init__(self):
            self.calls = 0
        async def execute(self, *a, **k):
            self.calls += 1
            raise Exception("boom")

    pool = _AlwaysFailPool()
    ws = {"cut": 3, "cut_anchor": "a", "cut_prev_anchor": "p",
          "seed_sig": "s", "summarized_through": 2, "dropped_total": 1}
    with patch.object(app, "pool", pool):
        result = _run(app._window_persist("sess-b13", dict(ws)))
    assert result is None, "persist must return None, never raise"


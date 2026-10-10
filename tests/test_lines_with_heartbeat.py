"""Tests for _lines_with_heartbeat: the persistent-task SSE line iterator.

The old pattern used asyncio.wait_for(anext(aiter), timeout=...) which CANCELLED
the underlying aiter_lines() generator on timeout, causing StopAsyncIteration on
the next call and a false 'upstream closed the stream early' diagnosis.

The new _lines_with_heartbeat keeps ONE persistent pending task and never cancels
it on timeout - it only yields None to signal a heartbeat is due.
"""

import asyncio
import json
import os
import sys
import time
from unittest.mock import MagicMock

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


def _make_resp(lines, delay=0.0):
    """Mock httpx response with aiter_lines() returning an async gen.

    If delay > 0, each line is preceded by an asyncio.sleep(delay) to simulate
    a slow upstream.
    """
    resp = MagicMock()
    resp.status_code = 200

    async def aiter():
        for line in lines:
            if delay > 0:
                await asyncio.sleep(delay)
            yield line

    resp.aiter_lines = lambda: aiter()
    return resp


async def _collect(gen, max_items=50):
    """Collect all items from an async generator, with a safety cap."""
    items = []
    async for item in gen:
        items.append(item)
        if len(items) >= max_items:
            break
    return items


def test_helper_exists():
    """The _lines_with_heartbeat function must exist in proxy.app."""
    assert hasattr(app, "_lines_with_heartbeat"), "_lines_with_heartbeat not found in proxy.app"
    import inspect
    assert inspect.isasyncgenfunction(app._lines_with_heartbeat), "_lines_with_heartbeat must be an async generator function"


def test_yields_lines_in_order():
    """Lines are yielded in the same order as the upstream."""
    lines = [
        'data: {"id": "s1", "choices": [{"delta": {"content": "hello"}}]}',
        'data: {"id": "s1", "choices": [{"delta": {"content": " world"}}]}',
        'data: [DONE]',
    ]
    resp = _make_resp(lines)
    items = _run(_collect(app._lines_with_heartbeat(resp, 5.0)))
    # All items should be the original lines (no None since data flows fast)
    assert all(item is not None for item in items), f"expected no None items, got: {items}"
    assert items == lines, f"expected {lines}, got {items}"


def test_normal_end_no_heartbeat():
    """A stream that ends normally (StopAsyncIteration) must not produce None."""
    lines = ['data: {"choices": []}', 'data: [DONE]']
    resp = _make_resp(lines)
    items = _run(_collect(app._lines_with_heartbeat(resp, 5.0)))
    assert None not in items, f"normal end should not yield None: {items}"
    assert len(items) == 2


def test_empty_stream():
    """An empty stream (immediate StopAsyncIteration) yields nothing."""
    resp = _make_resp([])
    items = _run(_collect(app._lines_with_heartbeat(resp, 0.1)))
    assert items == [], f"empty stream should yield nothing, got: {items}"


def test_heartbeat_on_slow_stream():
    """When upstream is silent longer than the interval, None is yielded (heartbeat due).

    This is the critical bug-fix scenario: the old code CANCELLED the aiter on
    timeout, causing StopAsyncIteration. The new code keeps the task alive.
    """
    # Simulate: 2 lines with a 0.3s gap, interval = 0.1s
    # Expected: line1, None (heartbeat), line2
    lines = ['data: first', 'data: second']
    resp = _make_resp(lines, delay=0.3)
    items = _run(_collect(app._lines_with_heartbeat(resp, 0.1)))

    # Filter out None to get the actual data lines
    data_lines = [item for item in items if item is not None]
    heartbeats = [item for item in items if item is None]

    assert data_lines == lines, f"data lines mismatch: {data_lines}"
    assert len(heartbeats) >= 1, f"expected at least 1 heartbeat, got {len(heartbeats)}: {items}"


def test_no_cancel_on_timeout():
    """THE KEY TEST: after a timeout (heartbeat), the next anext must still work.

    The old bug: asyncio.wait_for cancelled the inner anext(), which finalized
    the aiter_lines() generator. The next anext() raised StopAsyncIteration.

    The fix: _lines_with_heartbeat keeps the pending task alive across timeouts.
    """
    # 3 lines, each 0.25s apart, interval = 0.1s
    # Without the fix: after first timeout, the generator is dead -> only 1 line
    # With the fix: all 3 lines arrive, with heartbeats in between
    lines = ['data: one', 'data: two', 'data: three']
    resp = _make_resp(lines, delay=0.25)
    items = _run(_collect(app._lines_with_heartbeat(resp, 0.1)))

    data_lines = [item for item in items if item is not None]
    assert data_lines == lines, f"all 3 lines must arrive (no cancel), got: {data_lines}"


def test_multiple_timeouts_same_stream():
    """Multiple consecutive timeouts must not kill the stream."""
    # 2 lines, 0.4s apart, interval = 0.1s -> at least 3 heartbeats between them
    lines = ['data: a', 'data: b']
    resp = _make_resp(lines, delay=0.4)
    items = _run(_collect(app._lines_with_heartbeat(resp, 0.1)))

    data_lines = [item for item in items if item is not None]
    heartbeats = sum(1 for item in items if item is None)

    assert data_lines == lines, f"both lines must arrive: {data_lines}"
    assert heartbeats >= 2, f"expected >=2 heartbeats for 0.4s gap with 0.1s interval, got {heartbeats}"


def test_interleaved_heartbeat_and_data():
    """Heartbeats (None) and data lines are correctly interleaved."""
    lines = ['data: x', 'data: y']
    resp = _make_resp(lines, delay=0.2)
    items = _run(_collect(app._lines_with_heartbeat(resp, 0.1)))

    # The sequence should be: 'data: x', [None...], 'data: y'
    # Find positions
    x_idx = items.index('data: x')
    y_idx = items.index('data: y')
    assert x_idx < y_idx, "order must be preserved"
    # Between x and y there should be at least one None
    between = items[x_idx + 1:y_idx]
    assert any(item is None for item in between), f"expected heartbeat between lines: {items}"

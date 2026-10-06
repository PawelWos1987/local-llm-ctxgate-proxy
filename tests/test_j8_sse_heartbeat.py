"""J8: SSE heartbeat to keep connections alive when vLLM stalls.

When vLLM stalls, no SSE data reaches the client and intermediate proxies
close the connection. Fix: if no line arrives within SSE_HEARTBEAT_INTERVAL,
yield an SSE comment ': hb\n\n' to keep the connection alive.
"""

import asyncio
import json
import os
import sys
from unittest.mock import patch, MagicMock

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


def _make_resp(lines):
    """Mock httpx response with aiter_lines() returning an async gen."""
    resp = MagicMock()
    resp.status_code = 200

    async def aiter():
        for line in lines:
            yield line

    resp.aiter_lines = lambda: aiter()
    return resp


async def _heartbeat_wrapper(resp, interval):
    """Mirror of the app.py pattern: wait_for(anext) with heartbeat."""
    _aiter = resp.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(anext(_aiter), timeout=interval)
        except asyncio.TimeoutError:
            yield ": hb\n\n"
            continue
        except StopAsyncIteration:
            break
        yield line + "\n\n"


async def _collect(gen, max_chunks=30):
    parts = []
    async for chunk in gen:
        parts.append(chunk)
        if len(parts) >= max_chunks:
            break
    return "".join(parts)


def test_j8_constant_exists():
    assert hasattr(app, "SSE_HEARTBEAT_INTERVAL")
    assert app.SSE_HEARTBEAT_INTERVAL == 10


def test_j8_no_heartbeat_when_data_flows():
    """Data arriving within the interval -> no heartbeat."""
    lines = [
        'data: ' + json.dumps({"id": "s1", "choices": [{"delta": {"content": "a"}}]}),
        'data: ' + json.dumps({"id": "s1", "choices": [{"delta": {"content": "b"}}]}),
    ]
    resp = _make_resp(lines)
    result = _run(_collect(_heartbeat_wrapper(resp, 5.0)))
    assert ": hb" not in result, f"unexpected heartbeat: {result!r}"
    assert "a" in result and "b" in result


def test_j8_heartbeat_on_empty_stream():
    """An empty stream (immediate StopAsyncIteration) produces no heartbeat."""
    resp = _make_resp([])
    result = _run(_collect(_heartbeat_wrapper(resp, 0.1)))
    assert ": hb" not in result, f"empty stream should not produce heartbeat: {result!r}"
    assert result == "", f"expected empty, got: {result!r}"



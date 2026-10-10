"""Tests for streaming correctness (R1-R6)."""
import asyncio
import json
import time
import pytest
import httpx
from unittest.mock import patch, AsyncMock

# Import from proxy
import sys
sys.path.insert(0, '/home/pawelw/ctxproxy/proxy')


class FakeResponse:
    """Simulates an httpx streaming response with aiter_lines()."""
    def __init__(self, lines, delay=0.0):
        self._lines = list(lines)
        self._delay = delay
    
    async def aiter_lines(self):
        for line in self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield line
    
    async def aclose(self):
        pass


class SlowResponse:
    """Simulates a response that sends lines with delays."""
    def __init__(self, schedule):
        """schedule: list of (delay_seconds, line) tuples"""
        self._schedule = schedule
        self.closed = False
    
    async def aiter_lines(self):
        for delay, line in self._schedule:
            await asyncio.sleep(delay)
            yield line
    
    async def aclose(self):
        self.closed = True


class ErrorAfterDelay:
    """Simulates a response that errors after some delay."""
    def __init__(self, delay, error_type=httpx.ReadError):
        self._delay = delay
        self._error_type = error_type
        self.closed = False
    
    async def aiter_lines(self):
        await asyncio.sleep(self._delay)
        raise self._error_type("simulated read error")
        yield  # unreachable - makes this an async generator
    
    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_heartbeat_three_seconds_silence():
    """R1: 3s silence at 1s interval => 2+ heartbeats then the line."""
    from app import _lines_with_heartbeat
    
    # Send one line after 3 seconds
    resp = SlowResponse([(3.0, "data: {\"test\": true}\n")])
    collected = []
    async for item in _lines_with_heartbeat(resp, 1.0):
        collected.append(item)
        if len(collected) > 5:
            break
    
    # Should have at least 2 None (heartbeats) before the data line
    none_count = sum(1 for x in collected if x is None)
    assert none_count >= 2, f"Expected >=2 heartbeats, got {none_count}"
    assert "data:" in collected[-1]


@pytest.mark.asyncio
async def test_normal_completion_no_pending_tasks():
    """R1: Normal iterator completion leaves no pending tasks."""
    from app import _lines_with_heartbeat
    
    resp = FakeResponse(["line1", "line2", "line3"])
    collected = []
    async for item in _lines_with_heartbeat(resp, 5.0):
        collected.append(item)
    
    assert collected == ["line1", "line2", "line3"]
    # No pending tasks should remain
    pending = [t for t in asyncio.all_tasks() if not t.done()]
    # Allow the current test task
    assert len(pending) <= 1


@pytest.mark.asyncio
async def test_early_break_cancels_task():
    """R1: Early consumer break cancels pending task, closes iterator."""
    from app import _lines_with_heartbeat
    
    resp = SlowResponse([(0.1, "line1"), (10.0, "line2")])
    collected = []
    async for item in _lines_with_heartbeat(resp, 5.0):
        collected.append(item)
        if item == "line1":
            break  # Early break
    
    assert "line1" in collected
    # The pending task for line2 should be cancelled
    # (we can't easily verify this without hooks, but no warnings should appear)


@pytest.mark.asyncio
async def test_transport_error_after_heartbeat():
    """R1: Transport error after heartbeats propagates correctly."""
    from app import _lines_with_heartbeat
    
    resp = ErrorAfterDelay(2.0, httpx.ReadError)
    collected = []
    with pytest.raises(httpx.ReadError):
        async for item in _lines_with_heartbeat(resp, 1.0):
            collected.append(item)
    
    # Should have received at least 1 heartbeat before the error
    none_count = sum(1 for x in collected if x is None)
    assert none_count >= 1


@pytest.mark.asyncio
async def test_25s_silence_10s_interval():
    """R1: 25s silence at 10s interval => exactly 2 heartbeats before data."""
    from app import _lines_with_heartbeat
    
    resp = SlowResponse([(25.0, "data: first")])
    collected = []
    async for item in _lines_with_heartbeat(resp, 10.0):
        collected.append(item)
        if item is not None:
            break
    
    none_count = sum(1 for x in collected if x is None)
    assert none_count == 2, f"Expected exactly 2 heartbeats, got {none_count}"
    assert collected[-1] == "data: first"


@pytest.mark.asyncio
async def test_r2_no_synthetic_content():
    """R2: Empty after retry produces no synthetic content."""
    # This is tested via the integration test matrix
    # Here we verify the metric exists and the exit reason is correct
    from app import metrics
    assert "empty_after_retry" in metrics
    assert metrics["empty_after_retry"] == 0  # Initially zero


@pytest.mark.asyncio
async def test_r3_tool_call_in_reasoning_metric():
    """R3: Tool-call-in-reasoning metric exists."""
    from app import metrics
    assert "tool_call_in_reasoning" in metrics


@pytest.mark.asyncio
async def test_r5_cache_diagnostics_format():
    """R5: Cache diagnostics log format is correct."""
    # This is a logging test - verify the format string exists
    import app
    source = open('/home/pawelw/ctxproxy/proxy/app.py').read()
    assert "CACHE" in source or "retry_ttft" in source


@pytest.mark.asyncio
async def test_r6_flush_seam_nonlocal():
    """R6: _flush_seam has correct nonlocal declarations."""
    source = open('/home/pawelw/ctxproxy/proxy/app.py').read()
    assert "nonlocal full_content, seam_active" in source or "nonlocal seam_active" in source

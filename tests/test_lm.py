"""Tests for LM integration (R18, WI-3)."""
import asyncio
import pytest
import os
import sys
sys.path.insert(0, '/home/pawelw/ctxproxy/proxy')


@pytest.mark.asyncio
async def test_lm_disabled_no_calls():
    """R18: When LM_ENABLED=0, no LM calls are made."""
    os.environ["CTXGATE_LM_ENABLED"] = "0"
    import importlib
    import app
    importlib.reload(app)
    assert app.CTXGATE_LM_ENABLED == False


@pytest.mark.asyncio
async def test_lm_enabled_default():
    """R18: LM is enabled by default."""
    os.environ.pop("CTXGATE_LM_ENABLED", None)
    import importlib
    import app
    importlib.reload(app)
    assert app.CTXGATE_LM_ENABLED == True


@pytest.mark.asyncio
async def test_lm_limiter_no_leak_on_open_breaker():
    """WI-3: Open breaker does not leak limiter slots."""
    import importlib
    import app
    importlib.reload(app)
    
    # Simulate open breaker
    app._lm_breaker._failures = app._lm_breaker.threshold + 1
    import time as _t
    app._lm_breaker._open_at = _t.monotonic()
    
    # Try to make a call - should raise before acquiring slot
    initial_active = app._lm_rate_limiter._active
    
    with pytest.raises(Exception):
        await app._lm_do_call({"messages": [], "max_tokens": 10})
    
    # Active slots should be unchanged (no leak)
    assert app._lm_rate_limiter._active == initial_active


@pytest.mark.asyncio
async def test_unicode_escape_metric_exists():
    """WI-10: Unicode escape metric exists."""
    import importlib
    import app
    importlib.reload(app)
    assert "tool_args_unicode_escape" in app.metrics

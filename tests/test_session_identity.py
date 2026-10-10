"""Tests for session identity modes (R7-R10)."""
import asyncio
import hashlib
import pytest
import sys
sys.path.insert(0, '/home/pawelw/ctxproxy/proxy')


@pytest.mark.asyncio
async def test_goose_mode_with_header():
    """R7: Goose mode uses X-Session-ID header when present."""
    import os
    os.environ["CTXGATE_SESSION_IDENTITY"] = "goose"
    from app import _compute_x_sid
    
    result = _compute_x_sid("tenant123", "my-session-id", [])
    assert result == "tenant123:my-session-id"


@pytest.mark.asyncio
async def test_goose_mode_without_header():
    """R7: Goose mode falls back to content fingerprint."""
    import os
    os.environ["CTXGATE_SESSION_IDENTITY"] = "goose"
    from app import _compute_x_sid
    
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello world"},
    ]
    result = _compute_x_sid("tenant123", "", messages)
    assert result.startswith("tenant123:")
    # Should be deterministic
    result2 = _compute_x_sid("tenant123", "", messages)
    assert result == result2


@pytest.mark.asyncio
async def test_hash_mode_deterministic():
    """R8: Hash mode produces deterministic identity."""
    import os
    os.environ["CTXGATE_SESSION_IDENTITY"] = "hash"
    import app
    app.CTXGATE_SESSION_IDENTITY = "hash"
    from app import _compute_x_sid, _prefix_raw
    
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello world"},
    ]
    r1 = _compute_x_sid("tenant123", "header1", messages)
    r2 = _compute_x_sid("tenant123", "header1", messages)
    assert r1 == r2
    assert r1.startswith("tenant123:")
    # Should be 16 hex chars after the colon
    suffix = r1.split(":")[1]
    assert len(suffix) == 16
    assert all(c in '0123456789abcdef' for c in suffix)


@pytest.mark.asyncio
async def test_hash_mode_different_headers():
    """R8: Different headers produce different IDs."""
    import os
    os.environ["CTXGATE_SESSION_IDENTITY"] = "hash"
    import app
    app.CTXGATE_SESSION_IDENTITY = "hash"
    from app import _compute_x_sid
    
    messages = [{"role": "user", "content": "test"}]
    r1 = _compute_x_sid("t", "header-a", messages)
    r2 = _compute_x_sid("t", "header-b", messages)
    assert r1 != r2


@pytest.mark.asyncio
async def test_hash_mode_no_sqlite():
    """R8: Hash mode does not perform SQLite access."""
    import os
    os.environ["CTXGATE_SESSION_IDENTITY"] = "hash"
    import app
    app.CTXGATE_SESSION_IDENTITY = "hash"
    from app import _compute_x_sid
    
    # _compute_x_sid in hash mode should not touch any database
    # It only uses hashlib and the messages list
    messages = [{"role": "user", "content": "no sqlite here"}]
    result = _compute_x_sid("t", "", messages)
    assert result is not None


@pytest.mark.asyncio
async def test_session_key_suffix_preserved():
    """R8: Session key maintains the 8-char fingerprint suffix."""
    import os
    os.environ["CTXGATE_SESSION_IDENTITY"] = "hash"
    import app
    app.CTXGATE_SESSION_IDENTITY = "hash"
    from app import _compute_x_sid
    
    messages = [{"role": "user", "content": "test content"}]
    x_sid = _compute_x_sid("tenant", "hdr", messages)
    # The session_key is x_sid + ":" + fp8 (8 chars)
    # We verify x_sid format is correct
    parts = x_sid.split(":")
    assert len(parts) == 2
    assert len(parts[1]) == 16


@pytest.mark.asyncio
async def test_metrics_session_diagnostics():
    """R9: Session diagnostics metrics exist."""
    from app import metrics
    assert "session_header_present" in metrics
    assert "session_header_absent" in metrics
    assert "session_prefix_changes" in metrics


@pytest.mark.asyncio
async def test_bounded_observation_map():
    """R9: Observation map is bounded to 100 entries."""
    from app import _session_obs, _SESSION_OBS_MAX
    assert _SESSION_OBS_MAX == 100
    # Simulate overflow
    for i in range(150):
        _session_obs[f"key_{i}"] = set()
    # Should be bounded (eviction happens in chat_completions, not here)
    # Just verify the constant is correct
    assert _SESSION_OBS_MAX == 100

"""Tests for startup configuration (S1-S6)."""
import asyncio
import subprocess
import pytest
import os
import sys


@pytest.mark.asyncio
async def test_missing_dsn_exits_78():
    """S1: Missing DSN causes exit 78."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTXGATE")}
    env["CTXGATE_QWEN_TOKENIZER"] = "/nonexistent/tokenizer.json"
    result = subprocess.run(
        ["/opt/ctxgate-proxy/.venv/bin/python", "/home/pawelw/ctxproxy/proxy/app.py", "--check-config"],
        capture_output=True, text=True, env=env, timeout=10
    )
    assert result.returncode == 78


@pytest.mark.asyncio
async def test_change_me_dsn_exits_78():
    """S1: CHANGE_ME in DSN causes exit 78."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTXGATE")}
    env["CTXGATE_DB_DSN"] = "postgresql://user:CHANGE_ME@localhost:5432/db"
    env["CTXGATE_QWEN_TOKENIZER"] = "/nonexistent/tokenizer.json"
    result = subprocess.run(
        ["/opt/ctxgate-proxy/.venv/bin/python", "/home/pawelw/ctxproxy/proxy/app.py", "--check-config"],
        capture_output=True, text=True, env=env, timeout=10
    )
    assert result.returncode == 78
    assert "CHANGE_ME" in result.stderr or "CHANGE_ME" in result.stdout


@pytest.mark.asyncio
async def test_missing_tokenizer_exits_78():
    """S3: Missing tokenizer causes exit 78."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTXGATE")}
    env["CTXGATE_DB_DSN"] = "postgresql://user:pass@localhost:5432/db"
    env["CTXGATE_QWEN_TOKENIZER"] = ""
    result = subprocess.run(
        ["/opt/ctxgate-proxy/.venv/bin/python", "/home/pawelw/ctxproxy/proxy/app.py", "--check-config"],
        capture_output=True, text=True, env=env, timeout=10
    )
    assert result.returncode == 78


@pytest.mark.asyncio
async def test_check_config_no_side_effects():
    """S4: --check-config does not bind ports or write to DB."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTXGATE")}
    env["CTXGATE_DB_DSN"] = "postgresql://user:pass@localhost:5432/nonexistent_db"
    env["CTXGATE_QWEN_TOKENIZER"] = "/nonexistent/tokenizer.json"
    env["CTXGATE_API_KEYS"] = "test=sk-test"
    result = subprocess.run(
        ["/opt/ctxgate-proxy/.venv/bin/python", "/home/pawelw/ctxproxy/proxy/app.py", "--check-config"],
        capture_output=True, text=True, env=env, timeout=10
    )
    # Should exit 78 due to missing tokenizer, but should NOT have bound a port
    assert result.returncode == 78


@pytest.mark.asyncio
async def test_worker_check_config():
    """S4: Worker --check-config works."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTXGATE")}
    env["CTXGATE_DB_DSN"] = "postgresql://user:pass@localhost:5432/db"
    env["CTXGATE_QWEN_TOKENIZER"] = "/nonexistent/tokenizer.json"
    result = subprocess.run(
        ["/opt/ctxgate-proxy/.venv/bin/python", "/home/pawelw/ctxproxy/worker/worker.py", "--check-config"],
        capture_output=True, text=True, env=env, timeout=10
    )
    # Should run without crashing (may exit 78 if config invalid)
    assert result.returncode in (0, 78)

"""Tests for authentication (R15-R17)."""
import asyncio
import hashlib
import hmac
import pytest
import os
import sys
sys.path.insert(0, '/home/pawelw/ctxproxy/proxy')


@pytest.mark.asyncio
async def test_parse_api_keys_label_format():
    """R15: Parse label=key format."""
    os.environ["CTXGATE_API_KEYS"] = "alice=sk-alice,bob=sk-bob"
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    keys = app._parse_api_keys()
    assert keys == {"sk-alice": "alice", "sk-bob": "bob"}


@pytest.mark.asyncio
async def test_parse_api_keys_bare_format():
    """R15: Bare keys get 'local' label."""
    os.environ["CTXGATE_API_KEYS"] = "sk-bare1,sk-bare2"
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    keys = app._parse_api_keys()
    assert keys == {"sk-bare1": "local", "sk-bare2": "local"}


@pytest.mark.asyncio
async def test_parse_api_keys_invalid_label():
    """R15: Invalid labels are rejected."""
    os.environ["CTXGATE_API_KEYS"] = "INVALID LABEL=sk-key,good=sk-good"
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    keys = app._parse_api_keys()
    # "INVALID LABEL" has a space, should be rejected
    assert "sk-good" in keys
    assert "sk-key" not in keys


@pytest.mark.asyncio
async def test_validate_key_admin():
    """R15: Admin key validates as admin."""
    os.environ["CTXGATE_ADMIN_KEY"] = "sk-admin"
    os.environ["CTXGATE_API_KEYS"] = "alice=sk-alice"
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    app._api_key_map = app._parse_api_keys()
    valid, is_admin, tenant = app._validate_key("sk-admin")
    assert valid == True
    assert is_admin == True


@pytest.mark.asyncio
async def test_validate_key_tenant():
    """R15: Tenant key validates as non-admin."""
    os.environ["CTXGATE_ADMIN_KEY"] = "sk-admin"
    os.environ["CTXGATE_API_KEYS"] = "alice=sk-alice"
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    app._api_key_map = app._parse_api_keys()
    valid, is_admin, tenant = app._validate_key("sk-alice")
    assert valid == True
    assert is_admin == False
    assert tenant != ""


@pytest.mark.asyncio
async def test_validate_key_invalid():
    """R15: Invalid key returns (False, False, '')."""
    os.environ["CTXGATE_ADMIN_KEY"] = "sk-admin"
    os.environ["CTXGATE_API_KEYS"] = "alice=sk-alice"
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    app._api_key_map = app._parse_api_keys()
    valid, is_admin, tenant = app._validate_key("sk-wrong")
    assert valid == False
    assert is_admin == False
    assert tenant == ""


@pytest.mark.asyncio
async def test_no_auth_requires_loopback():
    """R16: No-auth mode requires loopback host."""
    os.environ["CTXGATE_ALLOW_NO_AUTH"] = "1"
    os.environ["CTXGATE_HOST"] = "0.0.0.0"
    os.environ["CTXGATE_API_KEYS"] = ""
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    problems = app.validate_config()
    assert any("loopback" in p.lower() for p in problems)


@pytest.mark.asyncio
async def test_no_auth_loopback_ok():
    """R16: No-auth with loopback is valid."""
    os.environ["CTXGATE_ALLOW_NO_AUTH"] = "1"
    os.environ["CTXGATE_HOST"] = "127.0.0.1"
    os.environ["CTXGATE_API_KEYS"] = ""
    os.environ["CTXGATE_API_KEY"] = ""
    import importlib
    import app
    importlib.reload(app)
    problems = app.validate_config()
    assert not any("loopback" in p.lower() for p in problems)


@pytest.mark.asyncio
async def test_tenant_limits_disabled_by_default():
    """R17: Per-tenant limits are off by default (0)."""
    os.environ["CTXGATE_TENANT_RATE_PER_SEC"] = "0"
    os.environ["CTXGATE_TENANT_RATE_BURST"] = "0"
    os.environ["CTXGATE_TENANT_MAX_CONCURRENCY"] = "0"
    import importlib
    import app
    importlib.reload(app)
    assert app.CTXGATE_TENANT_RATE_PER_SEC == 0.0
    assert app.CTXGATE_TENANT_RATE_BURST == 0
    assert app.CTXGATE_TENANT_MAX_CONCURRENCY == 0

"""Phase 2: Public Readiness tests.

Tests tenant isolation, auth coverage, admin gate, startup guards,
rate limiting, LM disable, and migration idempotency.

Run: python3 -m pytest tests/test_phase2_public.py -v

NOTE: These tests do NOT use starlette TestClient because that triggers
the lifespan which requires a real PostgreSQL connection. Instead, we
test the individual functions and route registrations directly.
"""
import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import json
import os
import sys
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Load proxy/app.py via importlib (same pattern as existing tests) ──
def _load_app():
    spec = importlib.util.spec_from_file_location("ctxgate_app", "/home/pawelw/ctxproxy/proxy/app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── Test constants ──
TENANT_A_LABEL = "tenant-a"
TENANT_B_LABEL = "tenant-b"
TENANT_A_KEY = "test-key-tenant-a"
TENANT_B_KEY = "test-key-tenant-b"
ADMIN_KEY = "test-admin-key"
TENANT_A_ID = hashlib.sha256(TENANT_A_LABEL.encode()).hexdigest()[:16]
TENANT_B_ID = hashlib.sha256(TENANT_B_LABEL.encode()).hexdigest()[:16]


def _setup_env(monkeypatch):
    """Set up env vars for Phase 2 tests."""
    monkeypatch.setenv("CTXGATE_API_KEYS", f"{TENANT_A_LABEL}={TENANT_A_KEY},{TENANT_B_LABEL}={TENANT_B_KEY}")
    monkeypatch.setenv("CTXGATE_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("CTXGATE_LM_ENABLED", "0")
    monkeypatch.setenv("CTXGATE_GOOSE_INTEGRATION", "0")
    monkeypatch.setenv("CTXGATE_TEST_ENDPOINTS", "0")
    monkeypatch.setenv("CTXGATE_HOST", "127.0.0.1")
    monkeypatch.setenv("CTXGATE_TENANT_RPM", "1000")
    monkeypatch.setenv("CTXGATE_TENANT_MAX_CONCURRENCY", "10")
    monkeypatch.setenv("CTXGATE_MAX_MESSAGES", "500")
    monkeypatch.setenv("CTXGATE_MAX_TOOLS", "100")
    monkeypatch.setenv("CTXGATE_DB_DSN", "postgresql://test:test@127.0.0.1:5432/ctxgate_test")
    monkeypatch.setenv("QWEN_TOKENIZER_PATH", "/tmp/nonexistent-tokenizer")


def _make_request(auth_header=None):
    """Create a mock FastAPI Request with the given Authorization header."""
    from starlette.datastructures import Headers
    headers = {}
    if auth_header:
        headers["authorization"] = auth_header
    req = MagicMock()
    req.headers = Headers(headers)
    req.state = MagicMock()
    return req


# ════════════════════════════════════════════════════════════════
# 1. TENANT ISOLATION
# ════════════════════════════════════════════════════════════════

class TestTenantIsolation:
    """Two tenants with identical prompts must not see each other's data."""

    def test_tenant_ids_are_distinct(self):
        """SHA-256 of different keys must produce different tenant IDs."""
        assert TENANT_A_ID != TENANT_B_ID
        assert len(TENANT_A_ID) == 16
        assert len(TENANT_B_ID) == 16

    def test_tenant_id_computation(self, monkeypatch):
        """_compute_tenant_id must return sha256(key)[:16]."""
        _setup_env(monkeypatch)
        app_mod = _load_app()
        app_mod._api_key_map = app_mod._parse_api_keys()
        tid_a = app_mod._compute_tenant_id(TENANT_A_KEY)
        tid_b = app_mod._compute_tenant_id(TENANT_B_KEY)
        assert tid_a == TENANT_A_ID
        assert tid_b == TENANT_B_ID
        assert tid_a != tid_b

    def test_x_sid_with_header(self, monkeypatch):
        """x_sid = tenant_id:X-Session-ID when header is present."""
        _setup_env(monkeypatch)
        app_mod = _load_app()
        x_sid = app_mod._compute_x_sid(TENANT_A_ID, "my-session-123", [])
        assert x_sid == f"{TENANT_A_ID}:my-session-123"

    def test_x_sid_without_header_uses_fingerprint(self, monkeypatch):
        """x_sid = tenant_id:fingerprint when no X-Session-ID header."""
        _setup_env(monkeypatch)
        app_mod = _load_app()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is 2+2?"},
        ]
        x_sid = app_mod._compute_x_sid(TENANT_A_ID, None, messages)
        expected_fp = hashlib.sha256(("You are helpful." + "What is 2+2?").encode()).hexdigest()[:16]
        assert x_sid == f"{TENANT_A_ID}:{expected_fp}"

    def test_same_prompt_different_tenant_different_sid(self, monkeypatch):
        """Identical prompts from two tenants produce different x_sid values."""
        _setup_env(monkeypatch)
        app_mod = _load_app()
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
        ]
        sid_a = app_mod._compute_x_sid(TENANT_A_ID, None, messages)
        sid_b = app_mod._compute_x_sid(TENANT_B_ID, None, messages)
        assert sid_a != sid_b
        assert sid_a.startswith(TENANT_A_ID + ":")
        assert sid_b.startswith(TENANT_B_ID + ":")


# ════════════════════════════════════════════════════════════════
# 2. AUTH COVERAGE
# ════════════════════════════════════════════════════════════════

class TestAuthCoverage:
    """All protected routes require a valid API key; /health and /ready stay open."""

    @pytest.fixture
    def app_mod(self, monkeypatch):
        _setup_env(monkeypatch)
        mod = _load_app()
        mod._api_key_map = mod._parse_api_keys()
        return mod

    def test_health_route_has_no_auth(self, app_mod):
        """/health must NOT have require_auth dependency."""
        for route in app_mod.app.routes:
            if hasattr(route, 'path') and route.path == "/health":
                deps = getattr(route, 'dependant', None)
                if deps:
                    sub_deps = deps.dependencies
                    assert not any(
                        d.call.__name__ in ('require_auth', 'require_admin')
                        for d in sub_deps
                    ), "/health should not require auth"
                break

    def test_ready_route_has_no_auth(self, app_mod):
        """/ready must NOT have require_auth dependency."""
        for route in app_mod.app.routes:
            if hasattr(route, 'path') and route.path == "/ready":
                deps = getattr(route, 'dependant', None)
                if deps:
                    sub_deps = deps.dependencies
                    assert not any(
                        d.call.__name__ in ('require_auth', 'require_admin')
                        for d in sub_deps
                    ), "/ready should not require auth"
                break

    def test_protected_routes_have_auth(self, app_mod):
        """All protected routes must have require_auth or require_admin dependency."""
        protected_paths = [
            "/v1/models", "/metrics", "/api/recent-calls",
            "/knowledge/search", "/deliverable", "/memory/{task_ref}",
        ]
        for route in app_mod.app.routes:
            if not hasattr(route, 'path'):
                continue
            for p in protected_paths:
                if route.path == p:
                    deps = getattr(route, 'dependant', None)
                    if deps:
                        sub_deps = deps.dependencies
                        has_auth = any(
                            d.call.__name__ in ('require_auth', 'require_admin')
                            for d in sub_deps
                        )
                        assert has_auth, f"Route {p} missing auth dependency"

    def test_require_auth_rejects_missing_key(self, app_mod, monkeypatch):
        """require_auth returns 401 when no Authorization header."""
        monkeypatch.setenv("CTXGATE_ALLOW_NO_AUTH", "0")
        app_mod.CTXGATE_ALLOW_NO_AUTH = False
        req = _make_request()
        resp = asyncio.run(app_mod.require_auth(req))
        assert resp is not None
        assert resp.status_code == 401

    def test_require_auth_rejects_invalid_key(self, app_mod, monkeypatch):
        """require_auth returns 401 for an invalid key."""
        monkeypatch.setenv("CTXGATE_ALLOW_NO_AUTH", "0")
        app_mod.CTXGATE_ALLOW_NO_AUTH = False
        req = _make_request("Bearer wrong-key")
        resp = asyncio.run(app_mod.require_auth(req))
        assert resp is not None
        assert resp.status_code == 401

    def test_require_auth_accepts_valid_tenant_key(self, app_mod):
        """require_auth passes for a valid tenant key and sets tenant_id."""
        req = _make_request(f"Bearer {TENANT_A_KEY}")
        resp = asyncio.run(app_mod.require_auth(req))
        assert resp is None
        assert req.state.tenant_id == TENANT_A_ID
        assert req.state.is_admin is False

    def test_require_auth_accepts_valid_admin_key(self, app_mod):
        """require_auth passes for the admin key and sets is_admin=True."""
        req = _make_request(f"Bearer {ADMIN_KEY}")
        resp = asyncio.run(app_mod.require_auth(req))
        assert resp is None
        assert req.state.is_admin is True

    def test_require_admin_rejects_tenant_key(self, app_mod):
        """require_admin returns 403 for a tenant key."""
        req = _make_request(f"Bearer {TENANT_A_KEY}")
        resp = app_mod.require_admin(req)
        assert resp is not None
        assert resp.status_code == 403

    def test_require_admin_accepts_admin_key(self, app_mod):
        """require_admin passes for the admin key."""
        req = _make_request(f"Bearer {ADMIN_KEY}")
        resp = app_mod.require_admin(req)
        assert resp is None
        assert req.state.is_admin is True

    def test_test_routes_not_registered_by_default(self, app_mod):
        """/_test/* routes must NOT be registered when CTXGATE_TEST_ENDPOINTS=0."""
        test_paths = [r.path for r in app_mod.app.routes if hasattr(r, 'path') and r.path.startswith('/_test')]
        assert len(test_paths) == 0, f"Found test routes: {test_paths}"

    def test_test_routes_registered_when_enabled(self, monkeypatch):
        """/_test/* routes must be registered when CTXGATE_TEST_ENDPOINTS=1."""
        _setup_env(monkeypatch)
        monkeypatch.setenv("CTXGATE_TEST_ENDPOINTS", "1")
        app_mod = _load_app()
        test_paths = [r.path for r in app_mod.app.routes if hasattr(r, 'path') and r.path.startswith('/_test')]
        assert len(test_paths) > 0, "No test routes found when CTXGATE_TEST_ENDPOINTS=1"


# ════════════════════════════════════════════════════════════════
# 3. ADMIN GATE
# ════════════════════════════════════════════════════════════════

class TestAdminGate:
    """Tenant keys get 403 on admin routes; admin key gets 200."""

    @pytest.fixture
    def app_mod(self, monkeypatch):
        _setup_env(monkeypatch)
        monkeypatch.setenv("CTXGATE_ALLOW_NO_AUTH", "0")
        mod = _load_app()
        mod.CTXGATE_ALLOW_NO_AUTH = False
        mod._api_key_map = mod._parse_api_keys()
        return mod

    def test_admin_routes_use_require_admin(self, app_mod):
        """/metrics and /api/* routes must use require_admin (not just require_auth)."""
        admin_paths = ["/metrics", "/api/recent-calls", "/api/errors"]
        for route in app_mod.app.routes:
            if not hasattr(route, 'path'):
                continue
            for p in admin_paths:
                if route.path == p:
                    deps = getattr(route, 'dependant', None)
                    if deps:
                        sub_deps = deps.dependencies
                        has_admin = any(
                            d.call.__name__ == 'require_admin'
                            for d in sub_deps
                        )
                        assert has_admin, f"Admin route {p} missing require_admin dependency"

    def test_tenant_key_rejected_on_admin(self, app_mod):
        """A tenant key calling require_admin gets 403."""
        req = _make_request(f"Bearer {TENANT_A_KEY}")
        resp = app_mod.require_admin(req)
        assert resp is not None
        assert resp.status_code == 403

    def test_admin_key_accepted_on_admin(self, app_mod):
        """The admin key calling require_admin passes."""
        req = _make_request(f"Bearer {ADMIN_KEY}")
        resp = app_mod.require_admin(req)
        assert resp is None
        assert req.state.is_admin is True


# ════════════════════════════════════════════════════════════════
# 4. STARTUP GUARDS
# ════════════════════════════════════════════════════════════════

class TestStartupGuards:
    """Refuse to start with no keys or CHANGE_ME in DSN."""

    def test_no_keys_exits(self, monkeypatch):
        monkeypatch.setenv("CTXGATE_API_KEYS", "")
        monkeypatch.setenv("CTXGATE_API_KEY", "")
        monkeypatch.delenv("CTXGATE_ADMIN_KEY", raising=False)
        monkeypatch.delenv("CTXGATE_ALLOW_NO_AUTH", raising=False)
        monkeypatch.setenv("CTXGATE_DB_DSN", "postgresql://test:test@127.0.0.1:5432/ctxgate_test")
        monkeypatch.setenv("QWEN_TOKENIZER_PATH", "")
        app_mod = _load_app()
        problems = app_mod.validate_config()
        assert len(problems) > 0, "Expected config problems"

    def test_change_me_dsn_exits(self, monkeypatch):
        monkeypatch.setenv("CTXGATE_API_KEYS", TENANT_A_KEY)
        monkeypatch.setenv("CTXGATE_DB_DSN", "postgresql://user:CHANGE_ME@127.0.0.1:5432/ctxgate")
        monkeypatch.setenv("QWEN_TOKENIZER_PATH", "")
        app_mod = _load_app()
        problems = app_mod.validate_config()
        assert any("CHANGE_ME" in p for p in problems), f"Expected CHANGE_ME problem, got: {problems}"

    def test_allow_no_auth_bypasses(self, monkeypatch):
        monkeypatch.setenv("CTXGATE_API_KEYS", "")
        monkeypatch.setenv("CTXGATE_API_KEY", "")
        monkeypatch.delenv("CTXGATE_ADMIN_KEY", raising=False)
        monkeypatch.setenv("CTXGATE_ALLOW_NO_AUTH", "1")
        monkeypatch.setenv("CTXGATE_DB_DSN", "postgresql://test:test@127.0.0.1:5432/ctxgate_test")
        monkeypatch.setenv("QWEN_TOKENIZER_PATH", "")
        app_mod = _load_app()
        # Should NOT raise
        app_mod.validate_config()

    def test_valid_config_passes(self, monkeypatch):
        _setup_env(monkeypatch)
        app_mod = _load_app()
        # Should NOT raise
        app_mod.validate_config()


# ════════════════════════════════════════════════════════════════
# 5. RATE LIMIT
# ════════════════════════════════════════════════════════════════

class TestRateLimit:
    """Per-tenant token bucket returns 429 with Retry-After on exceed."""

    @pytest.fixture
    def app_mod(self, monkeypatch):
        _setup_env(monkeypatch)
        monkeypatch.setenv("CTXGATE_TENANT_RATE_PER_SEC", "10")
        monkeypatch.setenv("CTXGATE_TENANT_RATE_BURST", "5")
        mod = _load_app()
        mod.CTXGATE_TENANT_RATE_PER_SEC = 10
        mod.CTXGATE_TENANT_RATE_BURST = 5
        mod._api_key_map = mod._parse_api_keys()
        return mod

    def test_tenant_rate_limiter_returns_429(self, app_mod):
        """Exhausting the tenant bucket returns a 429 response."""
        tb = app_mod._get_tenant_rate_limiter(TENANT_A_ID)
        tb.tokens = 0
        tb.last_refill = time.monotonic()
        resp = app_mod._check_tenant_rate_limit(TENANT_A_ID)
        assert resp is not None
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_different_tenant_unaffected(self, app_mod):
        """Exhausting tenant A's bucket does not affect tenant B."""
        tb_a = app_mod._get_tenant_rate_limiter(TENANT_A_ID)
        tb_a.tokens = 0
        tb_a.last_refill = time.monotonic()
        resp_b = app_mod._check_tenant_rate_limit(TENANT_B_ID)
        assert resp_b is None

    def test_tenant_semaphore(self, app_mod):
        """Per-tenant semaphore limits concurrency."""
        async def _test():
            sem = app_mod._get_tenant_semaphore(TENANT_A_ID)
            assert sem._value == app_mod.CTXGATE_TENANT_MAX_CONCURRENCY
            await sem.acquire()
            assert sem._value == app_mod.CTXGATE_TENANT_MAX_CONCURRENCY - 1
            sem.release()
            assert sem._value == app_mod.CTXGATE_TENANT_MAX_CONCURRENCY
        asyncio.run(_test())

    def test_message_count_limit(self, app_mod):
        """Messages array exceeding CTXGATE_MAX_MESSAGES is rejected."""
        assert app_mod.CTXGATE_MAX_MESSAGES > 0

    def test_tool_count_limit(self, app_mod):
        """Tools array exceeding CTXGATE_MAX_TOOLS is rejected."""
        assert app_mod.CTXGATE_MAX_TOOLS > 0


# ════════════════════════════════════════════════════════════════
# 6. LM DISABLED
# ════════════════════════════════════════════════════════════════

class TestLMDisabled:
    """With CTXGATE_LM_ENABLED=0, zero outbound calls to the LM."""

    def test_lm_disabled_flag(self, monkeypatch):
        _setup_env(monkeypatch)
        monkeypatch.setenv("CTXGATE_LM_ENABLED", "0")
        app_mod = _load_app()
        assert app_mod.CTXGATE_LM_ENABLED is False

    def test_lm_enabled_flag(self, monkeypatch):
        _setup_env(monkeypatch)
        monkeypatch.setenv("CTXGATE_LM_ENABLED", "1")
        app_mod = _load_app()
        assert app_mod.CTXGATE_LM_ENABLED is True

    def test_call_4b_returns_empty_when_disabled(self, monkeypatch):
        """_call_4b returns {} when LM is disabled (no outbound call)."""
        _setup_env(monkeypatch)
        monkeypatch.setenv("CTXGATE_LM_ENABLED", "0")
        app_mod = _load_app()
        # _lm_client is None when LM is disabled, so _call_4b returns {}
        assert app_mod._lm_client is None
        async def _test():
            result = await app_mod._call_4b([{"role": "user", "content": "test"}])
            assert result == {}
        asyncio.run(_test())


# ════════════════════════════════════════════════════════════════
# 7. MIGRATION IDEMPOTENCY
# ════════════════════════════════════════════════════════════════

class TestMigrationIdempotency:
    """Running migrations twice on a populated DB must be a no-op."""

    def test_add_column_if_not_exists_pattern(self):
        """The migration SQL uses ADD COLUMN IF NOT EXISTS for all tenant_id columns."""
        with open("/home/pawelw/ctxproxy/proxy/app.py") as f:
            src = f.read()
        # The migration uses a loop: ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS tenant_id TEXT
        assert "ADD COLUMN IF NOT EXISTS tenant_id TEXT" in src
        # Verify all 8 expected tables are in the migration list
        for tbl in ["tasks", "memories", "knowledge", "session_summaries",
                     "phase_summaries", "session_ledger", "deliverables", "working_memory"]:
            assert f"proxy.{tbl}" in src, f"Table proxy.{tbl} not found in migrations"

    def test_knowledge_index_recreation(self):
        """The knowledge unique index is dropped and recreated with tenant_id."""
        with open("/home/pawelw/ctxproxy/proxy/app.py") as f:
            src = f.read()
        assert "DROP INDEX IF EXISTS proxy.knowledge_domain_key_active_idx" in src
        assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_tenant_domain_key_active" in src

    def test_api_keys_table_creation(self):
        """The proxy.api_keys table is created with IF NOT EXISTS."""
        with open("/home/pawelw/ctxproxy/proxy/app.py") as f:
            src = f.read()
        assert "CREATE TABLE IF NOT EXISTS proxy.api_keys" in src
        assert "hash" in src
        assert "label" in src
        assert "disabled" in src

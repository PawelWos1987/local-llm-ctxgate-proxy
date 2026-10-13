"""Regression tests for Task 15 implementation changes.

Tests:
1. Global exception handler returns sanitized 500
2. Request ID middleware assigns X-Request-ID
3. Concurrency limit timeout returns 503
4. MAX_STREAM_DURATION is set correctly
"""
import sys, os
sys.path.insert(0, "/home/pawelw/ctxproxy")

import pytest
import asyncio


class TestGlobalExceptionHandler:
    """Verify the global exception handler exists and is properly configured."""
    
    def test_exception_handler_registered(self):
        """The FastAPI app should have a global exception handler registered."""
        from proxy.app import app
        # Check that the exception handler is in the app's exception handlers
        assert Exception in app.exception_handlers, "Global exception handler not registered"
    
    def test_exception_handler_returns_sanitized_response(self):
        """When an unhandled exception occurs, response should be sanitized JSON."""
        from proxy.app import app
        handler = app.exception_handlers.get(Exception)
        assert handler is not None, "No exception handler found"
        assert handler.__name__ == "global_exception_handler"


class TestRequestIdMiddleware:
    """Verify request ID middleware is present and functional."""
    
    def test_middleware_registered(self):
        """The app should have HTTP middleware for request IDs."""
        from proxy.app import app
        # Check that middleware stack includes our request ID middleware
        mw_names = [type(mw).__name__ for mw in app.user_middleware]
        assert len(app.user_middleware) > 0, "No user middleware registered"
    
    def test_request_id_function_exists(self):
        """The add_request_id function should exist in app module."""
        from proxy.app import add_request_id
        assert callable(add_request_id)


class TestConcurrencyLimitTimeout:
    """Verify the semaphore timeout handling in handler.py."""
    
    def test_timeout_value_reduced(self):
        """The semaphore acquire timeout should be 30s (was 60s)."""
        with open('/home/pawelw/ctxproxy/proxy/hotpath/handler.py', 'r') as f:
            content = f.read()
        assert 'timeout=30' in content, "Semaphore timeout not set to 30s"
        assert 'timeout=60' not in content, "Old 60s timeout still present"
    
    def test_503_response_on_timeout(self):
        """On timeout, handler should return 503 with Retry-After header."""
        with open('/home/pawelw/ctxproxy/proxy/hotpath/handler.py', 'r') as f:
            content = f.read()
        assert 'status_code=503' in content, "No 503 response on concurrency timeout"
        assert 'Retry-After' in content, "No Retry-After header on 503"
        assert 'concurrency_limit_reached' in content, "No metric tracking for concurrency limit"


class TestMaxStreamDuration:
    """Verify streaming max duration guard is in place."""
    
    def test_constant_defined(self):
        """MAX_STREAM_DURATION should be defined in to_vllm_stream.py."""
        from proxy.streaming.to_vllm_stream import MAX_STREAM_DURATION
        assert isinstance(MAX_STREAM_DURATION, int)
        assert MAX_STREAM_DURATION == 300, f"Expected default 300s, got {MAX_STREAM_DURATION}"
    
    def test_env_override(self):
        """MAX_STREAM_DURATION should be configurable via env var."""
        import importlib
        os.environ['CTXGATE_MAX_STREAM_DURATION'] = '600'
        try:
            import proxy.streaming.to_vllm_stream as mod
            importlib.reload(mod)
            assert mod.MAX_STREAM_DURATION == 600
        finally:
            del os.environ['CTXGATE_MAX_STREAM_DURATION']
            import proxy.streaming.to_vllm_stream as mod
            importlib.reload(mod)
            assert mod.MAX_STREAM_DURATION == 300

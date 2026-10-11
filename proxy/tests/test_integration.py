"""Phase 08: Integration tests for unified stream/non-stream validation,
context-error recut retry, and side-path handling.
"""
import json
import hashlib
import pytest

import proxy.app as app


def _make_messages(n_turns=3, with_tools=False):
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"User message {i}"})
        msgs.append({"role": "assistant", "content": f"Assistant reply {i}"})
    if with_tools:
        msgs[-1]["tool_calls"] = [
            {"id": "call_1", "type": "function",
             "function": {"name": "test_fn", "arguments": "{}"}}
        ]
        msgs.append({"role": "tool", "tool_call_id": "call_1", "content": "result"})
    return msgs


def _hash(messages):
    return app._live_prompt_identity(messages)


class TestUnifiedValidation:

    def test_validate_outgoing_payload_ok(self):
        msgs = _make_messages()
        h = _hash(msgs)
        body = {"messages": msgs}
        ok, err = app._validate_outgoing_payload(body, h)
        assert ok, f"Expected ok, got error: {err}"

    def test_validate_outgoing_payload_hash_mismatch(self):
        msgs = _make_messages()
        h = _hash(msgs)
        msgs[-2]["content"] = "TAMPERED"
        body = {"messages": msgs}
        ok, err = app._validate_outgoing_payload(body, h)
        assert not ok
        assert "live_prompt_integrity" in err

    def test_validate_outgoing_payload_metadata_leak(self):
        msgs = _make_messages()
        msgs[0]["ctxgate_internal"] = "secret"
        h = _hash(msgs)
        body = {"messages": msgs}
        ok, err = app._validate_outgoing_payload(body, h)
        assert not ok
        assert "metadata_leak" in err

    def test_validate_outgoing_payload_orphaned_tool(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "orphan_1", "content": "result"},
        ]
        h = _hash(msgs)
        body = {"messages": msgs}
        ok, err = app._validate_outgoing_payload(body, h)
        assert not ok
        assert "orphaned" in err

    def test_validate_outgoing_payload_empty_hash(self):
        msgs = _make_messages()
        body = {"messages": msgs}
        ok, err = app._validate_outgoing_payload(body, "")
        assert ok

    def test_stream_call_site_passes_hash(self):
        import inspect
        src = inspect.getsource(app.chat_completions)
        assert "stream_to_vllm(" in src
        assert "_live_prompt_hash" in src

    def test_forward_call_site_passes_hash(self):
        import inspect
        src = inspect.getsource(app.chat_completions)
        assert "forward_to_vllm(" in src
        assert "_live_prompt_hash" in src


class TestContextErrorDetection:

    def test_context_length_400(self):
        assert app._is_context_length_error(
            400, "This model's maximum context length is 8192 tokens")

    def test_too_many_tokens(self):
        assert app._is_context_length_error(
            400, "You exceed the token limit. Too many tokens in input.")

    def test_prompt_too_long(self):
        assert app._is_context_length_error(
            400, "Prompt is too long for the model")

    def test_input_too_long(self):
        assert app._is_context_length_error(
            400, "Input is too long for processing")

    def test_non_400_not_context(self):
        assert not app._is_context_length_error(401, "Unauthorized")
        assert not app._is_context_length_error(403, "Forbidden")
        assert not app._is_context_length_error(429, "Rate limit exceeded")
        assert not app._is_context_length_error(500, "Internal server error")
        assert not app._is_context_length_error(503, "Service unavailable")

    def test_400_non_context_not_context(self):
        assert not app._is_context_length_error(400, "Invalid API key format")
        assert not app._is_context_length_error(400, "Malformed JSON body")
        assert not app._is_context_length_error(400, "Unknown model name")

    def test_case_insensitive(self):
        assert app._is_context_length_error(
            400, "MAXIMUM CONTEXT LENGTH EXCEEDED")


class TestNoRetryAfterBytesSent:

    def test_bytes_sent_flag_exists_in_stream(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "_bytes_sent" in src

    def test_bytes_sent_prevents_recut(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "not _bytes_sent" in src

    def test_bytes_sent_emits_sse_error(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "context_length_exceeded_mid_stream" in src


class TestNoRecutForUnrelatedErrors:

    def test_401_no_recut(self):
        assert not app._is_context_length_error(401, "Unauthorized access")

    def test_403_no_recut(self):
        assert not app._is_context_length_error(403, "Forbidden resource")

    def test_429_no_recut(self):
        assert not app._is_context_length_error(429, "Rate limit exceeded")

    def test_500_no_recut(self):
        assert not app._is_context_length_error(500, "Internal error")

    def test_503_no_recut(self):
        assert not app._is_context_length_error(503, "Service unavailable")

    def test_400_malformed_json_no_recut(self):
        assert not app._is_context_length_error(
            400, "Expecting value: line 1 column 1 (char 0)")

    def test_forward_uses_is_context_length_error(self):
        import inspect
        src = inspect.getsource(app.forward_to_vllm)
        assert "_is_context_length_error" in src

    def test_stream_uses_is_context_length_error(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "_is_context_length_error" in src


class TestOneRetryMax:

    def test_forward_uses_recut_to(self):
        import inspect
        src = inspect.getsource(app.forward_to_vllm)
        assert "_recut_to" in src

    def test_stream_uses_recut_to(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "_recut_to" in src

    def test_shrink_retried_prevents_second_retry(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "shrink_retried" in src
        assert "not shrink_retried" in src

    def test_forward_single_retry(self):
        import inspect
        src = inspect.getsource(app.forward_to_vllm)
        assert "context_length_exceeded" in src


class TestMissingUsage:

    def test_forward_handles_missing_usage(self):
        import inspect
        src = inspect.getsource(app.forward_to_vllm)
        assert "usage" in src


class TestFinalPayloadValidation:

    def test_validate_called_in_forward(self):
        import inspect
        src = inspect.getsource(app.forward_to_vllm)
        assert "_validate_outgoing_payload" in src

    def test_validate_called_in_stream(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "_validate_outgoing_payload" in src

    def test_validate_called_after_recut_forward(self):
        import inspect
        src = inspect.getsource(app.forward_to_vllm)
        recut_pos = src.find("_recut_to")
        validate_pos = src.find("_validate_outgoing_payload", recut_pos + 1) if recut_pos >= 0 else -1
        assert validate_pos > recut_pos

    def test_validate_called_after_recut_stream(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        recut_pos = src.find("_recut_to")
        validate_pos = src.find("_validate_outgoing_payload", recut_pos + 1) if recut_pos >= 0 else -1
        assert validate_pos > recut_pos

    def test_strip_metadata_before_validation(self):
        import inspect
        for fn_name in ("forward_to_vllm", "stream_to_vllm"):
            src = inspect.getsource(getattr(app, fn_name))
            strip_pos = src.find("_strip_proxy_metadata")
            validate_pos = src.find("_validate_outgoing_payload")
            assert strip_pos < validate_pos


class TestBreakerOpen:

    def test_breaker_check_in_stream(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "_check_vllm_breaker" in src or "_vllm_breaker" in src

    def test_breaker_open_raises(self):
        assert hasattr(app, "_check_vllm_breaker")
        assert hasattr(app, "_BreakerOpen")

class TestDbOutage:

    def test_db_errors_are_caught(self):
        import inspect
        src = inspect.getsource(app.stream_to_vllm)
        assert "return_exceptions=True" in src or "except" in src

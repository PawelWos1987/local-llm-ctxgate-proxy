"""Suite: regression test for the stream 400-retry UnboundLocalError.

The 400-retry edit reassigned `input_tokens` inside the nested `generate()`
stream generator, which made it a local to generate() (Python scoping). Every
read of the enclosing value -- the usage chunk, the final usage/_record_call,
and the metrics totals -- then raised:

    UnboundLocalError: cannot access local variable 'input_tokens'

These tests drive the real `stream_to_vllm` generator to completion through a
stub httpx transport (no real vLLM) and assert:
  (a) a normal 200 stream with a usage chunk completes,
  (b) 400 (containing 'max_tokens') then 200  -> exactly one retry, and the
      final usage prompt_tokens equals the SHRUNK count,
  (c) 400 twice -> a single SSE error line, no exception,
  (d) an exception inside the generator -> the terminal error handler does not
      raise UnboundLocalError.

Run with:  python -m pytest tests/test_stream_retry_scoping.py -v
"""
import asyncio
import importlib.util
import json
import os
import sys

import httpx

# --- Import proxy/app.py by path (no package import; module runs standalone). ---
_APP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "proxy", "app.py"))


def _load_app_module():
    spec = importlib.util.spec_from_file_location("ctxgate_app_under_test", _APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ctxgate_app_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


app = _load_app_module()


# --------------------------------------------------------------------------- #
# Stub httpx transport
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, body=b"", lines=None):
        self.status_code = status_code
        self._body = body
        self._lines = lines if lines is not None else []

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for l in self._lines:
            yield l


class FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RaisingStream:
    """A 'response factory' that raises a bare Exception when client.stream()
    is called (not an httpx.TransportError, so it escapes the inner handlers)."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, *a, **k):
        raise self._exc


class StubClient:
    """Pops scripted responses in order; records every client.stream() call."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0
        self.bodies = []

    def stream(self, method, url, json=None):
        self.calls += 1
        self.bodies.append(json)
        item = self._scripted.pop(0) if self._scripted else FakeResponse(200, lines=["data: [DONE]"])
        if isinstance(item, RaisingStream):
            return item()
        return FakeStreamCtx(item)


# --------------------------------------------------------------------------- #
# SSE builders
# --------------------------------------------------------------------------- #
def _reasoning_stream(reasoning_text):
    """A 200 stream that emits only reasoning (no content) and ends with
    finish_reason='stop' -> triggers the empty-response -> reasoning_overflow
    -> non-thinking retry path (the only route into the inner loop that
    contains the 400-retry)."""
    return [
        "data: " + json.dumps({"id": "r", "choices": [{"index": 0, "delta": {"reasoning_content": reasoning_text}, "finish_reason": None}]}),
        "data: " + json.dumps({"id": "r", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]


def _ok_stream(content="Hello there", completion_tokens=3):
    """A 200 stream with content + a usage chunk."""
    return [
        "data: " + json.dumps({"id": "ok", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}),
        "data: " + json.dumps({
            "id": "ok",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": completion_tokens,
                      "prompt_tokens_details": {"cached_tokens": 0}},
        }),
        "data: [DONE]",
    ]


def _err_400(message):
    return FakeResponse(400, body=message.encode("utf-8"))


def _nonlooping_reasoning(n=10):
    """A block of >500 chars of NON-repeating reasoning (so _detect_loop == 0)."""
    return " ".join(
        "Thought %02d explores a distinct path involving case %02d with unique detail %02d here." % (i, i, i)
        for i in range(n)
    )


# --------------------------------------------------------------------------- #
# Driver (sync wrapper around the async generator)
# --------------------------------------------------------------------------- #
async def _drive(scripted, input_tokens, messages, session_key="test-session"):
    app._vllm_client = StubClient(scripted)
    body = {
        "model": app.VLLM_MODEL,
        "messages": [dict(m) for m in messages],
        "max_tokens": 50,
        "stream": True,
    }
    resp = await app.stream_to_vllm(body, input_tokens, session_key)
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c)
    return app._vllm_client, chunks


def _drive_sync(scripted, input_tokens, messages, session_key="test-session"):
    return asyncio.run(_drive(scripted, input_tokens, messages, session_key))


def _parse(chunks):
    """Return list of parsed JSON objects (None for [DONE])."""
    parsed = []
    for c in chunks:
        for line in c.splitlines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                parsed.append(None)
            else:
                try:
                    parsed.append(json.loads(payload))
                except (json.JSONDecodeError, ValueError):
                    parsed.append({"__raw__": payload})
    return parsed


def _reset_breaker():
    app._vllm_breaker._failures = 0
    app._vllm_breaker._open_at = 0.0


# --------------------------------------------------------------------------- #
# (a) normal 200 stream with usage chunk
# --------------------------------------------------------------------------- #
def test_a_normal_200_stream_with_usage():
    _reset_breaker()
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Please say hello to the user."},
    ]
    client, chunks = _drive_sync([FakeResponse(200, lines=_ok_stream())], 123, msgs)

    assert client.calls == 1
    parsed = _parse(chunks)
    assert parsed.count(None) == 1, "expected a single [DONE]"
    assert any(isinstance(p, dict) and p.get("choices", [{}])[0].get("delta", {}).get("content") == "Hello there"
               for p in parsed), "content delta missing"
    usage_chunks = [p for p in parsed if isinstance(p, dict) and p.get("usage")]
    assert usage_chunks, "no usage chunk emitted"
    assert usage_chunks[-1]["usage"]["prompt_tokens"] == 123
    assert usage_chunks[-1]["usage"]["total_tokens"] == 123 + 3
    assert not any(isinstance(p, dict) and "error" in p for p in parsed)


# --------------------------------------------------------------------------- #
# (b) 400 (max_tokens) then 200  -> exactly one retry; final usage == shrunk
# --------------------------------------------------------------------------- #
def test_b_400_max_tokens_then_200_one_retry_shrunk_usage():
    _reset_breaker()
    long_user = "A" * 3000
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": long_user},
    ]
    input_tokens = 500
    scripted = [
        FakeResponse(200, lines=_reasoning_stream(_nonlooping_reasoning())),
        _err_400('{"error":{"message":"maximum context length exceeded: max_tokens too large"}}'),
        FakeResponse(200, lines=_ok_stream()),
    ]
    client, chunks = _drive_sync(scripted, input_tokens, msgs)

    assert client.calls == 3, "expected 3 upstream calls, got %d" % client.calls
    parsed = _parse(chunks)
    assert not any(isinstance(p, dict) and "error" in p for p in parsed), "400 must be retried, not surfaced"
    assert parsed.count(None) == 1
    usage_chunks = [p for p in parsed if isinstance(p, dict) and p.get("usage")]
    assert usage_chunks, "no usage chunk after recovery"
    final_prompt = usage_chunks[-1]["usage"]["prompt_tokens"]

    normalized = app._normalize_system_messages([dict(m) for m in msgs])
    shrunk = app._emergency_shrink([dict(m) for m in normalized], int(input_tokens * 0.8))
    expected = app.count_messages_tokens(shrunk)

    assert final_prompt == expected, "final usage prompt_tokens %d != shrunk %d" % (final_prompt, expected)
    assert expected < input_tokens, "shrink did not reduce token count"


# --------------------------------------------------------------------------- #
# (c) 400 twice  -> a single SSE error line, no exception
# --------------------------------------------------------------------------- #
def test_c_400_twice_single_sse_error_no_exception():
    _reset_breaker()
    long_user = "B" * 3000
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": long_user},
    ]
    scripted = [
        FakeResponse(200, lines=_reasoning_stream(_nonlooping_reasoning())),
        _err_400('{"error":{"message":"max_tokens exceeds maximum"}}'),
        _err_400('{"error":{"message":"max_tokens exceeds maximum (again)"}}'),
    ]
    client, chunks = _drive_sync(scripted, 500, msgs)

    assert client.calls == 3
    parsed = _parse(chunks)
    error_lines = [p for p in parsed if isinstance(p, dict) and "error" in p]
    assert len(error_lines) == 1, "expected exactly ONE SSE error line, got %d" % len(error_lines)
    assert parsed.count(None) == 1
    assert "max_tokens" in json.dumps(error_lines[0])


# --------------------------------------------------------------------------- #
# (d) exception inside the generator  -> terminal handler must not UnboundLocalError
# --------------------------------------------------------------------------- #
def test_d_exception_inside_generator_terminal_handler_no_unbound():
    _reset_breaker()
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
    ]
    scripted = [RaisingStream(RuntimeError("boom inside generator"))]

    # Must complete WITHOUT raising UnboundLocalError.
    client, chunks = _drive_sync(scripted, 77, msgs)

    parsed = _parse(chunks)
    assert parsed.count(None) == 1, "terminal handler must end with [DONE]"
    stop_chunks = [p for p in parsed if isinstance(p, dict) and p.get("choices", [{}])[0].get("finish_reason") == "stop"]
    assert stop_chunks, "terminal stop chunk missing"
    assert not any(isinstance(p, dict) and "error" in p for p in parsed)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

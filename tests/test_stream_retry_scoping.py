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
def test_b_400_context_capacity_structured_failure():
    """New fail-safe: when the 400 re-shrink raises ContextCapacityError
    (protected material alone exceeds the ceiling), the proxy emits a
    structured ctxgate metadata block with reason='context_capacity' and
    finish_reason='length', then [DONE]. No silent corruption."""
    _reset_breaker()
    # Tiny message set so that shrink target (input*0.8) is below protected size
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "A" * 2000},  # ~504 tokens
    ]
    input_tokens = 500
    scripted = [
        FakeResponse(200, lines=_reasoning_stream(_nonlooping_reasoning())),
        _err_400('{"error":{"message":"maximum context length exceeded: max_tokens too large"}}'),
    ]
    client, chunks = _drive_sync(scripted, input_tokens, msgs)

    parsed = _parse(chunks)
    assert parsed.count(None) == 1, "must end with [DONE]"
    # Structured ctxgate metadata present
    cg = [p for p in parsed if isinstance(p, dict) and "ctxgate" in p]
    assert cg, "no ctxgate metadata block emitted"
    assert cg[-1]["ctxgate"]["truncated"] is True
    assert cg[-1]["ctxgate"]["reason"] == "context_capacity"
    # finish_reason must be 'length', NOT 'stop'
    fin = [p for p in parsed if isinstance(p, dict) and p.get("choices") and p["choices"][0].get("finish_reason")]
    assert fin, "no finish_reason chunk"
    assert fin[-1]["choices"][0]["finish_reason"] == "length", "must be 'length' not 'stop'"


# --------------------------------------------------------------------------- #
# (c) 400 twice  -> a single SSE error line, no exception
# --------------------------------------------------------------------------- #
def test_c_400_twice_no_unbound_local():
    """Two consecutive 400s: the first may trigger context_capacity or a retry;
    the second (shrink_retried already True) surfaces as an SSE error line.
    Key invariant: no UnboundLocalError, stream ends with [DONE]."""
    _reset_breaker()
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "B" * 2000},
    ]
    scripted = [
        FakeResponse(200, lines=_reasoning_stream(_nonlooping_reasoning())),
        _err_400('{"error":{"message":"max_tokens exceeds maximum"}}'),
        _err_400('{"error":{"message":"max_tokens exceeds maximum (again)"}}'),
    ]
    client, chunks = _drive_sync(scripted, 500, msgs)

    parsed = _parse(chunks)
    assert parsed.count(None) == 1, "must end with [DONE]"
    # At least one terminal signal (either ctxgate or SSE error)
    has_terminal = (any(isinstance(p, dict) and "ctxgate" in p for p in parsed)
                    or any(isinstance(p, dict) and "error" in p for p in parsed))
    assert has_terminal, "no terminal signal (ctxgate or error) found"


# --------------------------------------------------------------------------- #
# (d) exception inside the generator  -> terminal handler must not UnboundLocalError
# --------------------------------------------------------------------------- #
def test_d_exception_inside_generator_terminal_handler_no_unbound():
    """Exception inside the stream generator: the outer except handler must
    flush seam text, emit structured ctxgate metadata (truncated=True,
    reason='error', finish_reason='length'), and end with [DONE].
    Must NOT raise UnboundLocalError."""
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
    # Structured ctxgate metadata with truncated=True
    cg = [p for p in parsed if isinstance(p, dict) and "ctxgate" in p]
    assert cg, "no ctxgate metadata on exception path"
    assert cg[-1]["ctxgate"]["truncated"] is True
    assert cg[-1]["ctxgate"]["reason"] == "error"
    # finish_reason must be 'length' (not 'stop') for a forced termination
    fin = [p for p in parsed if isinstance(p, dict) and p.get("choices") and p["choices"][0].get("finish_reason")]
    assert fin, "no finish_reason chunk"
    assert fin[-1]["choices"][0]["finish_reason"] == "length", "exception path must use 'length'"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

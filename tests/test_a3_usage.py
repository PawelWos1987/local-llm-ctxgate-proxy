"""A3: non-stream usage KeyError in forward_to_vllm.

When vLLM omits 'usage' (or sends "usage": null), the three assignments
data["usage"]["completion_tokens"] / ["total_tokens"] / ["prompt_tokens"]
raise KeyError/TypeError and the client gets a 500 after a successful
generation. The fix ensures data["usage"] is a dict before those lines.
"""

import asyncio
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "  # space => skip tokenizer load

import proxy.app as app  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def post(self, url, json=None):
        return _Resp(self._payload)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_body(content):
    return {
        "messages": [{"role": "user", "content": "hello"}],
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def test_a3_usage_missing_key_returns_200():
    """vLLM response with NO 'usage' key -> must return 200, not 500."""
    body = _make_body("world")
    client = _FakeClient(body)
    with patch.object(app, "_vllm_client", client), patch.object(app, "vllm_alive", True):
        resp = _run(app.forward_to_vllm({"messages": [{"role": "user", "content": "hi"}]}, 5, "sess-a3"))
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    data = json.loads(resp.body)
    assert data["choices"][0]["message"]["content"] == "world"
    # When usage is absent, output_tokens defaults to 0
    assert data["usage"]["completion_tokens"] == 0
    assert data["usage"]["prompt_tokens"] == 5
    assert data["usage"]["total_tokens"] == 5


def test_a3_usage_null_returns_200():
    """vLLM response with 'usage': null -> must return 200, not 500/TypeError."""
    body = _make_body("world")
    body["usage"] = None
    client = _FakeClient(body)
    with patch.object(app, "_vllm_client", client), patch.object(app, "vllm_alive", True):
        resp = _run(app.forward_to_vllm({"messages": [{"role": "user", "content": "hi"}]}, 5, "sess-a3"))
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    data = json.loads(resp.body)
    assert data["choices"][0]["message"]["content"] == "world"
    # When usage is null, output_tokens defaults to 0
    assert data["usage"]["completion_tokens"] == 0
    assert data["usage"]["prompt_tokens"] == 5
    assert data["usage"]["total_tokens"] == 5


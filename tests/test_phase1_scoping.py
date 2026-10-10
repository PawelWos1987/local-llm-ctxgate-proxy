"""Phase 1: tool-call-in-reasoning + empty-after-retry tests."""
import asyncio
import importlib.util
import json
import os
import sys

import httpx
import pytest

os.environ["CTXGATE_MAX_REASONING_TOKENS"] = "100"

_APP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "proxy", "app.py"))


def _load_app_module():
    spec = importlib.util.spec_from_file_location("ctxgate_app_p1", _APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ctxgate_app_p1"] = mod
    spec.loader.exec_module(mod)
    return mod


app = _load_app_module()


def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _reasoning(text, sid="gen"):
    return _sse({"id": sid, "object": "chat.completion.chunk", "created": 0,
                 "model": app.VLLM_MODEL,
                 "choices": [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": None}]})


def _content(text, sid="gen"):
    return _sse({"id": sid, "object": "chat.completion.chunk", "created": 0,
                 "model": app.VLLM_MODEL,
                 "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})


def _finish(fr, sid="gen"):
    return _sse({"id": sid, "object": "chat.completion.chunk", "created": 0,
                 "model": app.VLLM_MODEL,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": fr}]})


DONE = "data: [DONE]\n\n"

# Build the param-close marker from char codes to avoid literal in source
PARAM_CLOSE = chr(60) + "/param" + "eter" + chr(62)


class FakeResp:
    def __init__(self, status=200, lines=None):
        self.status_code = status
        self._lines = lines or []
        self._i = 0

    async def aread(self):
        return b""

    async def aiter_lines(self):
        while self._i < len(self._lines):
            yield self._lines[self._i]
            self._i += 1


class FakeCtx:
    def __init__(self, r):
        self._r = r

    async def __aenter__(self):
        return self._r

    async def __aexit__(self, *a):
        return False


class MockClient:
    def __init__(self, seg1_lines, seg2_lines):
        self._s1 = seg1_lines
        self._s2 = seg2_lines
        self._n = 0

    def stream(self, method, url, json=None):
        self._n += 1
        if self._n == 1:
            return FakeCtx(FakeResp(200, self._s1))
        return FakeCtx(FakeResp(200, self._s2))


def _run(mock):
    app._vllm_client = mock
    body = {"messages": [{"role": "user", "content": "test"}], "stream": True}

    async def _go():
        resp = await app.stream_to_vllm(body, 100, "p1-test")
        events = []
        async for chunk in resp.body_iterator:
            t = chunk if isinstance(chunk, str) else chunk.decode()
            for line in t.strip().split("\n"):
                if line.startswith("data: "):
                    events.append(line[6:])
        return events

    return asyncio.run(_go())


def _parse(events):
    parsed = []
    for e in events:
        if e == "[DONE]":
            parsed.append({"_type": "done"})
        else:
            try:
                parsed.append(json.loads(e))
            except Exception:
                parsed.append({"_raw": e})
    return parsed


def _final_ctxgate(parsed):
    for p in reversed(parsed):
        if "ctxgate" in p:
            return p["ctxgate"]
    return None


def _all_content(parsed):
    parts = []
    for p in parsed:
        if p.get("_type") == "done" or "_raw" in p:
            continue
        choices = p.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            c = delta.get("content", "")
            if c:
                parts.append(c)
    return "".join(parts)


# --- Change 4: tool call in reasoning ---

def test_tool_call_in_reasoning_triggers_metric_and_retry():
    """Segment 1: reasoning with tool-call markup (param-close), finish=stop, no content.
    Segment 2: retry produces actual content.
    Assert: metric tool_call_in_reasoning >= 1, content delivered."""
    s1 = [
        _reasoning("thinking about the answer " + PARAM_CLOSE + " done"),
        _finish("stop"),
        DONE,
    ]
    s2 = [
        _content("hello from retry"),
        _finish("stop"),
        DONE,
    ]
    # Reset metric before test
    app.metrics["tool_call_in_reasoning"] = 0
    mock = MockClient(s1, s2)
    events = _run(mock)
    parsed = _parse(events)
    cg = _final_ctxgate(parsed)
    assert cg is not None, "No ctxgate payload"
    assert app.metrics["tool_call_in_reasoning"] >= 1, (
        f"metric tool_call_in_reasoning={app.metrics['tool_call_in_reasoning']}, expected >=1"
    )
    content = _all_content(parsed)
    assert "hello from retry" in content, f"content={content!r}"


# --- Change 5: empty after retry ---

def test_empty_after_retry_emits_placeholder():
    """Both segments produce no content, no tool_calls.
    Segment 1: reasoning overflow (5x40=200 chars > 100*3=300? No, 200<300).
    Actually with MAX_REASONING_TOKENS=100, threshold is 100*3=300 chars.
    Use 5x80=400 chars to trigger overflow.
    Segment 2: just finish=stop, no content.
    Assert: placeholder content, exit_reason=empty_after_retry."""
    s1 = [
        _reasoning("x" * 80),
        _reasoning("x" * 80),
        _reasoning("x" * 80),
        _reasoning("x" * 80),
        _reasoning("x" * 80),
        _finish("length"),
        DONE,
    ]
    s2 = [
        _finish("stop"),
        DONE,
    ]
    mock = MockClient(s1, s2)
    events = _run(mock)
    parsed = _parse(events)
    cg = _final_ctxgate(parsed)
    assert cg is not None, "No ctxgate payload"
    assert cg["reason"] == "empty_after_retry", f"reason={cg['reason']}"
    content = _all_content(parsed)
    # R2: No synthetic content - content must be empty
    assert content == "", f"content should be empty, got: {content!r}"

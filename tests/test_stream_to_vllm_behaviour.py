"""Fix 3 behavioural verification: retry flush survives interruption.

Drives stream_to_vllm() with a mocked _vllm_client whose SSE script is:
  Segment 1: reasoning-only stream that overflows (reasoning_chars > MAX_REASONING_TOKENS * 3)
  Segment 2: varies by test case

These four tests define Fix 3's contract.
"""
import asyncio
import importlib.util
import json
import os
import sys

import httpx
import pytest

# Set small reasoning threshold BEFORE import
os.environ["CTXGATE_MAX_REASONING_TOKENS"] = "100"

_APP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "proxy", "app.py"))


def _load_app_module():
    spec = importlib.util.spec_from_file_location("ctxgate_app_fix3", _APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ctxgate_app_fix3"] = mod
    spec.loader.exec_module(mod)
    return mod


app = _load_app_module()


def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _reasoning(text, sid="gen"):
    return _sse({"id": sid, "object": "chat.completion.chunk", "created": 0,
                 "model": app.VLLM_MODEL,
                 "choices": [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": None}]})


def _tc(piece, sid="gen"):
    return _sse({"id": sid, "object": "chat.completion.chunk", "created": 0,
                 "model": app.VLLM_MODEL,
                 "choices": [{"index": 0, "delta": {"tool_calls": piece}, "finish_reason": None}]})


def _finish(fr, sid="gen"):
    return _sse({"id": sid, "object": "chat.completion.chunk", "created": 0,
                 "model": app.VLLM_MODEL,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": fr}]})


DONE = "data: [DONE]\n\n"


class FakeResp:
    def __init__(self, status=200, lines=None, raise_after=False):
        self.status_code = status
        self._lines = lines or []
        self._raise_after = raise_after
        self._i = 0

    async def aread(self):
        return b""

    async def aiter_lines(self):
        while self._i < len(self._lines):
            yield self._lines[self._i]
            self._i += 1
        if self._raise_after:
            raise httpx.TransportError("simulated transport death")


class FakeCtx:
    def __init__(self, r):
        self._r = r
    async def __aenter__(self):
        return self._r
    async def __aexit__(self, *a):
        return False


class MockClient:
    def __init__(self, seg2_lines, seg2_raise=False):
        self._s1 = []
        for i in range(5):
            self._s1.append(_reasoning("x" * 40))
        self._s1.append(_finish("length"))
        self._s1.append(DONE)
        self._s2 = seg2_lines
        self._s2r = seg2_raise
        self._n = 0

    def stream(self, method, url, json=None):
        self._n += 1
        if self._n == 1:
            return FakeCtx(FakeResp(200, self._s1))
        return FakeCtx(FakeResp(200, self._s2, raise_after=self._s2r))


def safe_tc():
    return [
        _tc([{"index": 0, "id": "c1", "type": "function",
              "function": {"name": "execute_typescript", "arguments": ""}}]),
        _tc([{"index": 0, "function": {"arguments": '{"code": "async function run() { return 1; }"}'}}]),
    ]


def unsafe_tc():
    code = "while (s.includes(a)) { s = s.replace(a, b); }"
    return [
        _tc([{"index": 0, "id": "c1", "type": "function",
              "function": {"name": "execute_typescript", "arguments": ""}}]),
        _tc([{"index": 0, "function": {"arguments": json.dumps({"code": code})}}]),
    ]


def incomplete_tc():
    return [
        _tc([{"index": 0, "id": "c1", "type": "function",
              "function": {"name": "execute_typescript", "arguments": '{"code": "async'}}]),
    ]


def _run(mock):
    app._vllm_client = mock
    body = {"messages": [{"role": "user", "content": "test"}], "stream": True}

    async def _go():
        resp = await app.stream_to_vllm(body, 100, "fix3-test")
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
                d = json.loads(e)
                parsed.append(d)
            except Exception:
                parsed.append({"_raw": e})
    return parsed


def _final_ctxgate(parsed):
    for p in reversed(parsed):
        if "ctxgate" in p:
            return p["ctxgate"]
    return None


def _tc_deltas(parsed):
    result = []
    for p in parsed:
        if p.get("_type") == "done" or "_raw" in p:
            continue
        choices = p.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            tc = delta.get("tool_calls")
            if tc:
                result.append(tc)
    return result


def _terminal_positions(parsed):
    term_fr = None
    done_pos = None
    for i, p in enumerate(parsed):
        if p.get("_type") == "done":
            done_pos = i
        choices = p.get("choices", [])
        if choices:
            fr = choices[0].get("finish_reason")
            if fr:
                term_fr = (i, fr)
    return term_fr, done_pos


def test_safe_retry_transport_error():
    """Segment 2: complete safe execute_typescript, then TransportError."""
    seg2 = safe_tc() + [_finish("tool_calls"), DONE]
    mock = MockClient(seg2, seg2_raise=True)
    events = _run(mock)
    parsed = _parse(events)
    cg = _final_ctxgate(parsed)
    assert cg is not None, "No ctxgate payload found"
    assert cg["reason"] == "tool_calls_complete", f"reason={cg['reason']}"
    assert cg["truncated"] == False, f"truncated={cg['truncated']}"
    assert cg["tool_calls_complete"] == True, f"tool_calls_complete={cg['tool_calls_complete']}"
    assert cg["tool_calls_emitted"] >= 1, f"tool_calls_emitted={cg['tool_calls_emitted']}"
    assert parsed[-1].get("_type") == "done", "Last event is not [DONE]"
    # The retry path may emit finish_reason=tool_calls in the final chunk before [DONE].
    # Allow finish_reason on the last non-DONE event, but not on earlier ones.
    non_done = [p for p in parsed if p.get("_type") != "done"]
    for i, p in enumerate(non_done[:-1]):
        choices = p.get("choices", [])
        if choices:
            fr = choices[0].get("finish_reason")
            assert fr is None, f"Event {i} has premature finish_reason={fr}"


def test_unsafe_retry_transport_error():
    """Segment 2: complete unsafe execute_typescript, then TransportError."""
    seg2 = unsafe_tc() + [_finish("tool_calls"), DONE]
    mock = MockClient(seg2, seg2_raise=True)
    events = _run(mock)
    parsed = _parse(events)
    cg = _final_ctxgate(parsed)
    assert cg is not None, "No ctxgate payload found"
    assert cg["reason"] == "unsafe_loop_blocked", f"reason={cg['reason']}"
    assert cg["truncated"] == True, f"truncated={cg['truncated']}"
    assert cg["tool_calls_complete"] == False, f"tool_calls_complete={cg['tool_calls_complete']}"


def test_incomplete_retry_transport_error():
    """Segment 2: incomplete execute_typescript, then TransportError."""
    seg2 = incomplete_tc()
    mock = MockClient(seg2, seg2_raise=True)
    events = _run(mock)
    parsed = _parse(events)
    cg = _final_ctxgate(parsed)
    assert cg is not None, "No ctxgate payload found"
    assert cg["reason"] == "interrupted", f"reason={cg['reason']}"
    assert cg["truncated"] == True, f"truncated={cg['truncated']}"
    assert cg["tool_calls_complete"] == False, f"tool_calls_complete={cg['tool_calls_complete']}"


def test_clean_end_safe_tc():
    """Segment 2: complete safe execute_typescript, stream ends with [DONE]."""
    seg2 = safe_tc() + [_finish("tool_calls"), DONE]
    mock = MockClient(seg2, seg2_raise=False)
    events = _run(mock)
    parsed = _parse(events)
    cg = _final_ctxgate(parsed)
    assert cg is not None, "No ctxgate payload found"
    assert cg["reason"] == "tool_calls_complete", f"reason={cg['reason']}"
    assert cg["truncated"] == False, f"truncated={cg['truncated']}"
    assert cg["tool_calls_complete"] == True, f"tool_calls_complete={cg['tool_calls_complete']}"
    assert cg["tool_calls_emitted"] >= 1, f"tool_calls_emitted={cg['tool_calls_emitted']}"

"""Deterministic SSE fakes. Every fixture is a Python literal."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx


@dataclass
class DataChunk:
    payload: dict


@dataclass
class RawLine:
    text: str


@dataclass
class Heartbeat:
    pass


@dataclass
class Done:
    pass


@dataclass
class Raise:
    exc: Exception


@dataclass
class Close:
    pass


class FakeResponse:
    def __init__(self, script, status_code=200):
        self._script = list(script)
        self.status_code = status_code

    async def aread(self):
        return b""

    def aiter_lines(self):
        script = list(self._script)

        async def gen():
            for step in script:
                if isinstance(step, DataChunk):
                    yield "data: " + json.dumps(step.payload)
                elif isinstance(step, RawLine):
                    yield step.text
                elif isinstance(step, Heartbeat):
                    yield ""
                elif isinstance(step, Done):
                    yield "data: [DONE]"
                    return
                elif isinstance(step, Raise):
                    raise step.exc
                elif isinstance(step, Close):
                    return

        return gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeVLLM:
    def __init__(self, scripts):
        self._scripts = list(scripts)
        self._idx = 0
        self.calls = []

    def stream(self, method, url, json=None, **kw):
        self.calls.append({"method": method, "url": url, "json": json})
        script = self._scripts[min(self._idx, len(self._scripts) - 1)]
        self._idx += 1

        @asynccontextmanager
        async def cm():
            yield FakeResponse(script)

        return cm()


def content_delta(text, sid="c1"):
    return DataChunk({
        "id": sid, "object": "chat.completion.chunk", "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": {"content": text},
                     "finish_reason": None}],
    })


def reasoning_delta(text, sid="c1"):
    return DataChunk({
        "id": sid, "object": "chat.completion.chunk", "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": {"reasoning_content": text},
                     "finish_reason": None}],
    })


def toolcall_delta(name="", arguments="", index=0, call_id=None, sid="c1"):
    piece = {"index": index, "function": {}}
    if name:
        piece["function"]["name"] = name
    if arguments:
        piece["function"]["arguments"] = arguments
    if call_id:
        piece["id"] = call_id
        piece["type"] = "function"
    return DataChunk({
        "id": sid, "object": "chat.completion.chunk", "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": {"tool_calls": [piece]},
                     "finish_reason": None}],
    })


def finish_chunk(reason, sid="c1"):
    return DataChunk({
        "id": sid, "object": "chat.completion.chunk", "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": {},
                     "finish_reason": reason}],
    })


def usage_chunk(p, c):
    return DataChunk({
        "id": "c1", "object": "chat.completion.chunk", "created": 0,
        "model": "test", "choices": [],
        "usage": {"prompt_tokens": p, "completion_tokens": c,
                  "total_tokens": p + c,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    })


SAFE_TS_ARGS = '{"code": "async function run() { return 1; }"}'
UNSAFE_TS_ARGS = '{"code": "while (s.includes(A)) { s = s.replace(A, B); }"}'
INCOMPLETE_TS_ARGS = '{"code": "const x = 1;"'


def _split(s):
    n = len(s)
    a, b = n // 3, 2 * n // 3
    return [s[:a], s[a:b], s[b:]]


def script_reasoning_overflow():
    return [
        reasoning_delta("x" * 4000),
        finish_chunk("length"),
        usage_chunk(100, 4000),
        Done(),
    ]


def script_safe_tc_normal():
    p = _split(SAFE_TS_ARGS)
    return [
        toolcall_delta(name="execute_typescript", call_id="call_1",
                       index=0, arguments=p[0]),
        toolcall_delta(arguments=p[1], index=0),
        toolcall_delta(arguments=p[2], index=0),
        finish_chunk("tool_calls"),
        usage_chunk(100, 50),
        Done(),
    ]


def script_safe_tc_transport_dies():
    p = _split(SAFE_TS_ARGS)
    return [
        toolcall_delta(name="execute_typescript", call_id="call_1",
                       index=0, arguments=p[0]),
        toolcall_delta(arguments=p[1], index=0),
        toolcall_delta(arguments=p[2], index=0),
        Raise(httpx.TransportError("simulated")),
    ]


def script_unsafe_tc_normal():
    p = _split(UNSAFE_TS_ARGS)
    return [
        toolcall_delta(name="execute_typescript", call_id="call_1",
                       index=0, arguments=p[0]),
        toolcall_delta(arguments=p[1], index=0),
        toolcall_delta(arguments=p[2], index=0),
        finish_chunk("tool_calls"),
        usage_chunk(100, 50),
        Done(),
    ]


def script_unsafe_tc_transport_dies():
    p = _split(UNSAFE_TS_ARGS)
    return [
        toolcall_delta(name="execute_typescript", call_id="call_1",
                       index=0, arguments=p[0]),
        toolcall_delta(arguments=p[1], index=0),
        toolcall_delta(arguments=p[2], index=0),
        Raise(httpx.TransportError("simulated")),
    ]


def script_incomplete_tc_normal():
    p = _split(INCOMPLETE_TS_ARGS)
    return [
        toolcall_delta(name="execute_typescript", call_id="call_1",
                       index=0, arguments=p[0]),
        toolcall_delta(arguments=p[1], index=0),
        finish_chunk("length"),
        usage_chunk(100, 20),
        Done(),
    ]


def script_pure_text_normal():
    return [
        content_delta("Hello "),
        content_delta("world."),
        finish_chunk("stop"),
        usage_chunk(100, 10),
        Done(),
    ]


async def drive_stream_to_vllm(scripts):
    from proxy import app as P
    P._vllm_client = FakeVLLM(scripts)
    body = {
        "model": P.VLLM_MODEL,
        "messages": [{"role": "user", "content": "go"}],
        "max_tokens": 2048,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    resp = await P.stream_to_vllm(body, 200, "test-session")
    events = []
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        for line in chunk.split("\n"):
            if line.startswith("data: "):
                payload = line[6:]
                if payload == "[DONE]":
                    events.append({"kind": "done"})
                else:
                    try:
                        events.append({"kind": "chunk",
                                       "data": json.loads(payload)})
                    except Exception:
                        events.append({"kind": "raw", "data": payload})
            elif line.startswith(": "):
                events.append({"kind": "comment", "data": line})
    return events


def run_stream(scripts):
    return asyncio.run(drive_stream_to_vllm(scripts))


def toolcall_payloads(events):
    out = []
    for e in events:
        if e["kind"] != "chunk":
            continue
        for ch in e["data"].get("choices", []):
            delta = ch.get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                out.append(tc)
    return out


def terminal_event(events):
    for e in reversed(events):
        if e["kind"] != "chunk":
            continue
        for ch in e["data"].get("choices", []):
            if ch.get("finish_reason"):
                return e["data"]
    return None


def terminal_ctxgate(events):
    f = terminal_event(events)
    return (f or {}).get("ctxgate", {})

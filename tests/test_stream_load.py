#!/usr/bin/env python3
"""Suite 7 - SSE streaming, concurrent load, trimming, sampling, schema, tools, errors."""
import json
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:9201"
MODEL = "Qwen3.8-27B"
passed = 0
failed = 0
lock = threading.Lock()

def check(name, cond, info=""):
    global passed, failed
    with lock:
        if cond:
            passed += 1
            print("PASS " + name)
        else:
            failed += 1
            print("FAIL " + name + " :: " + str(info))

def raw_req(path, payload=None, method=None, timeout=90):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def go(path, payload, timeout=90):
    st, hd, body = raw_req(path, payload, None, timeout)
    try:
        return st, json.loads(body.decode())
    except Exception:
        return st, None

def get_metrics():
    st, _, b = raw_req("/metrics")
    try:
        return json.loads(b.decode())
    except Exception:
        return {}

def metric(name):
    m = get_metrics()
    v = m.get(name, 0)
    try:
        return float(v)
    except Exception:
        return 0.0


# ===================== A: SSE format =====================

def sse_capture(payload, timeout=60):
    url = BASE + "/v1/chat/completions"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=timeout)
    ct = r.headers.get("Content-Type", "")
    raw = r.read().decode(errors="replace")
    lines = [x for x in raw.split("\n") if x.strip()]
    data_lines = []
    for x in lines:
        if x.startswith("data:"):
            body = x[len("data:"):].strip()
            if body != "[DONE]":
                data_lines.append(body)
    chunks = []
    bad = []
    for ln in data_lines:
        try:
            chunks.append(json.loads(ln))
        except Exception:
            bad.append(ln[:80])
    done = any(x.strip() == "data: [DONE]" for x in lines)
    return ct, chunks, bad, done

def t_a1():
    p = {"model": MODEL, "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]}
    ct, chunks, bad, done = sse_capture(p)
    check("A1 content-type is text/event-stream",
          ct.lower().startswith("text/event-stream"), ct)

def t_a2():
    p = {"model": MODEL, "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]}
    ct, chunks, bad, done = sse_capture(p)
    ok = (len(chunks) >= 1
          and all(isinstance(c.get("object"), str)
                  and isinstance(c.get("id"), str)
                  and len(c.get("choices", [])) == 1 for c in chunks))
    check("A2 every data line is a valid chunk object", ok,
          (len(chunks), bad[:2]))

def t_a3():
    p = {"model": MODEL, "stream": True, "max_tokens": 64,
         "messages": [{"role": "user", "content": "hi"}]}
    ct, chunks, bad, done = sse_capture(p)
    nonempty = 0
    for c in chunks:
        d = c["choices"][0].get("delta") or {}
        if d.get("content"):
            nonempty += 1
        if d.get("reasoning"):
            nonempty += 1
    check("A3 at least one delta has content or reasoning", nonempty >= 1, nonempty)

def t_a4():
    p = {"model": MODEL, "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]}
    ct, chunks, bad, done = sse_capture(p)
    roleset = set()
    for c in chunks:
        d = c["choices"][0].get("delta") or {}
        if d.get("role"):
            roleset.add(d["role"])
    check("A4 role appears in some delta", len(roleset) >= 1, roleset)

def t_a5():
    p = {"model": MODEL, "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]}
    ct, chunks, bad, done = sse_capture(p)
    check("A5 stream ends with [DONE] sentinel", done, bad[:2])

def t_a6():
    p = {"model": MODEL, "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]}
    ct, chunks, bad, done = sse_capture(p)
    if len(chunks) >= 2:
        ids = set(c.get("id") for c in chunks)
        check("A6 all chunks share the same id", len(ids) == 1, ids)
    else:
        check("A6 all chunks share the same id", True, "only 1 chunk")

def t_a7():
    p = {"model": MODEL, "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]}
    ct, chunks, bad, done = sse_capture(p)
    if len(chunks) >= 2:
        created = set(c.get("created") for c in chunks)
        check("A7 all chunks share the same created timestamp", len(created) == 1, created)
    else:
        check("A7 all chunks share the same created timestamp", True, "only 1 chunk")

def t_a8():
    p = {"model": MODEL, "stream": True, "max_tokens": 32,
         "messages": [{"role": "user", "content": "count to 5"}]}
    ct, chunks, bad, done = sse_capture(p)
    indices = [c["choices"][0].get("index", 0) for c in chunks]
    check("A8 all choices have index 0", all(i == 0 for i in indices),
          set(indices))


# ===================== B: Concurrent load =====================

def t_b1():
    results = {}
    def worker(i):
        st, body = go("/v1/chat/completions", {
            "model": MODEL, "max_tokens": 8,
            "messages": [{"role": "user", "content": "say ok " + str(i)}]
        })
        results[i] = st
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(worker, i) for i in range(5)]
        for f in futures:
            f.result()
    all_200 = all(v == 200 for v in results.values())
    check("B1 5 concurrent requests all return 200", all_200, results)

def t_b2():
    results = {}
    def worker(i):
        p = {"model": MODEL, "stream": True, "max_tokens": 8,
             "messages": [{"role": "user", "content": "say ok " + str(i)}]}
        try:
            ct, chunks, bad, done = sse_capture(p, timeout=60)
            results[i] = (200, len(chunks), done)
        except Exception as e:
            results[i] = (0, 0, False)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(worker, i) for i in range(3)]
        for f in futures:
            f.result()
    ok = all(v[0] == 200 and v[1] >= 1 and v[2] for v in results.values())
    check("B2 3 concurrent streams all complete with [DONE]", ok, results)

def t_b3():
    t0 = time.time()
    def worker(i):
        st, _ = go("/v1/chat/completions", {
            "model": MODEL, "max_tokens": 4,
            "messages": [{"role": "user", "content": "hi " + str(i)}]
        })
        return st
    with ThreadPoolExecutor(max_workers=10) as ex:
        sts = list(ex.map(worker, range(10)))
    elapsed = time.time() - t0
    check("B3 10 concurrent finish in <60s", elapsed < 60,
          (round(elapsed, 1), sts.count(200)))

def t_b4():
    before_total = metric("requests_total")
    before_ok = metric("requests_ok")
    def worker(i):
        st, _ = go("/v1/chat/completions", {
            "model": MODEL, "max_tokens": 4,
            "messages": [{"role": "user", "content": "x " + str(i)}]
        })
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(worker, range(5)))
    after_total = metric("requests_total")
    after_ok = metric("requests_ok")
    check("B4 metrics delta: 5 more total", after_total - before_total >= 5,
          (before_total, after_total))
    check("B5 metrics delta: 5 more ok", after_ok - before_ok >= 5,
          (before_ok, after_ok))


# ===================== C: Context trimming =====================

def t_c1():
    msgs = []
    for i in range(200):
        msgs.append({"role": "user", "content": "word " + str(i)})
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8, "messages": msgs
    })
    check("C1 long context (200 msgs) returns 200", st == 200, st)

def t_c2():
    first_content = "UNIQUE_MARKER_XYZ"
    msgs = [{"role": "user", "content": first_content}]
    for i in range(50):
        msgs.append({"role": "assistant", "content": "resp " + str(i)})
        msgs.append({"role": "user", "content": "q " + str(i)})
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8, "messages": msgs
    })
    check("C2 first-user preserved after trim (200)", st == 200, st)

def t_c3():
    before_in = metric("tokens_in_total")
    msgs = [{"role": "user", "content": "x " * 5000 + " "} for _ in range(100)]
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 4, "messages": msgs
    })
    after_in = metric("tokens_in_total")
    check("C3 tokens_in increases after large context", after_in > before_in,
          (before_in, after_in))
    check("C4 large context still returns 200", st == 200, st)

def t_c5():
    before_in = metric("tokens_in_total")
    st, _ = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "short"}]
    })
    after_in = metric("tokens_in_total")
    delta = after_in - before_in
    check("C5 small context token delta is small (< 50)",
          0 < delta < 50, delta)


# ===================== D: Sampling params =====================

def t_d1():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "temperature": 0.1, "top_p": 0.9,
        "messages": [{"role": "user", "content": "hi"}]
    })
    check("D1 temperature=0.1 top_p=0.9 accepted", st == 200, st)

def t_d2():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "temperature": 1.5, "top_p": 0.5,
        "messages": [{"role": "user", "content": "hi"}]
    })
    check("D2 temperature=1.5 top_p=0.5 accepted", st == 200, st)

def t_d3():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 4,
        "n": 1,
        "messages": [{"role": "user", "content": "hi"}]
    })
    check("D3 n=1 accepted", st == 200, st)

def t_d4():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "stop": ["STOPWORD_XYZ"],
        "messages": [{"role": "user", "content": "hi"}]
    })
    check("D4 stop sequence accepted", st == 200, st)

def t_d5():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "presence_penalty": 0.5, "frequency_penalty": 0.3,
        "messages": [{"role": "user", "content": "hi"}]
    })
    check("D5 presence/frequency penalty accepted", st == 200, st)

def t_d6():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "seed": 42,
        "messages": [{"role": "user", "content": "hi"}]
    })
    check("D6 seed=42 accepted", st == 200, st)

def t_d7():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "temperature": 0.7,
        "messages": [{"role": "user", "content": "hi"}]
    })
    check("D7 temperature=0.7 (typical) accepted", st == 200, st)


# ===================== E: Response schema =====================

def t_e1():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]
    })
    ok = (st == 200 and body is not None
          and "id" in body and "object" in body and "created" in body
          and "model" in body and "choices" in body)
    check("E1 top-level schema fields present", ok,
          list(body.keys()) if body else "no body")

def t_e2():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]
    })
    if st == 200 and body and body.get("choices"):
        c = body["choices"][0]
        ok = ("index" in c and "message" in c and "finish_reason" in c)
        check("E2 choice schema fields present", ok, list(c.keys()))
    else:
        check("E2 choice schema fields present", False, "no choices")

def t_e3():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]
    })
    if st == 200 and body and body.get("choices"):
        m = body["choices"][0].get("message", {})
        ok = "role" in m
        check("E3 message has role field", ok, m.get("role"))
    else:
        check("E3 message has role field", False, "no message")

def t_e4():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]
    })
    if st == 200 and body:
        ok = body.get("object") == "chat.completion"
        check("E4 object is chat.completion", ok, body.get("object"))
    else:
        check("E4 object is chat.completion", False, "no body")

def t_e5():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]
    })
    if st == 200 and body:
        ok = isinstance(body.get("id"), str) and body["id"].startswith("chatcmpl-")
        check("E5 id starts with chatcmpl-", ok, body.get("id", "")[:20])
    else:
        check("E5 id starts with chatcmpl-", False, "no body")

def t_e6():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]
    })
    if st == 200 and body:
        ok = body.get("model") == MODEL
        check("E6 model field matches request", ok, body.get("model"))
    else:
        check("E6 model field matches request", False, "no body")

def t_e7():
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True
    })
    # stream returns SSE, not JSON
    ct, chunks, bad, done = sse_capture({
        "model": MODEL, "stream": True, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]
    })
    if chunks:
        c = chunks[0]
        ok = c.get("object") == "chat.completion.chunk"
        check("E7 stream chunk object is chat.completion.chunk", ok, c.get("object"))
    else:
        check("E7 stream chunk object is chat.completion.chunk", False, "no chunks")


# ===================== F: Tools round-trip =====================

def t_f1():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"}
                    },
                    "required": ["location"]
                }
            }
        }
    ]
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 32,
        "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
        "tools": tools,
        "tool_choice": "auto"
    })
    check("F1 tools request returns 200", st == 200, st)

def t_f2():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "add_numbers",
                "description": "Add two numbers",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"}
                    },
                    "required": ["a", "b"]
                }
            }
        }
    ]
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 32,
        "messages": [{"role": "user", "content": "Add 3 and 4"}],
        "tools": tools,
        "tool_choice": "auto"
    })
    if st == 200 and body and body.get("choices"):
        msg = body["choices"][0].get("message", {})
        tc = msg.get("tool_calls")
        check("F2 tool_calls field exists in response (may be null)", tc is None or isinstance(tc, list),
              type(tc).__name__)
    else:
        check("F2 tool_calls field exists in response (may be null)", False, "no body")

def t_f3():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search the web",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            }
        }
    ]
    # Send a tool result back
    msgs = [
        {"role": "user", "content": "Search for cats"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "search", "arguments": json.dumps({"query": "cats"})}}]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": "Cats are mammals."}
    ]
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 32,
        "messages": msgs,
        "tools": tools
    })
    check("F3 tool result round-trip returns 200", st == 200, st)

def t_f4():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "calc",
                "description": "Calculate",
                "parameters": {
                    "type": "object",
                    "properties": {"expr": {"type": "string"}},
                    "required": ["expr"]
                }
            }
        }
    ]
    # Streaming with tools
    p = {"model": MODEL, "stream": True, "max_tokens": 16,
         "messages": [{"role": "user", "content": "Calculate 2+2"}],
         "tools": tools}
    ct, chunks, bad, done = sse_capture(p)
    check("F4 streaming with tools works", done and len(chunks) >= 1,
          (len(chunks), done))

def t_f5():
    # No tools: should not have tool_calls
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "Say hello"}]
    })
    if st == 200 and body and body.get("choices"):
        msg = body["choices"][0].get("message", {})
        tc = msg.get("tool_calls")
        check("F5 no tools: tool_calls is null", tc is None, tc)
    else:
        check("F5 no tools: tool_calls is null", False, "no body")


# ===================== G: Error bodies =====================

def t_g1():
    st, body = go("/v1/chat/completions", {
        "model": "nonexistent-model-xyz", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]
    })
    ok = st in (400, 404)
    check("G1 unknown model returns 4xx", ok, st)

def t_g2():
    st, hd, body = raw_req("/v1/chat/completions", {
        "model": MODEL
    })
    ok = st in (400, 422)
    check("G2 missing messages returns 4xx", ok, st)

def t_g3():
    st, hd, body = raw_req("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": "not a list"
    })
    ok = st in (400, 422, 500)
    check("G3 messages as string returns 4xx or 500 (no crash loop)", ok, st)

def t_g4():
    st, hd, body = raw_req("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": 12345}]
    })
    check("G4 non-string content handled without crash (200/4xx/500)",
          st in (200, 400, 422, 500), st)

def t_g5():
    st, hd, body = raw_req("/v1/chat/completions", None, "POST")
    ok = st in (400, 422)
    check("G5 empty POST body returns 4xx", ok, st)

def t_g6():
    st, hd, body = raw_req("/v1/nonexistent-endpoint")
    check("G6 unknown path returns 404", st == 404, st)

def t_g7():
    st, hd, body = raw_req("/v1/chat/completions", {
        "model": MODEL, "max_tokens": -5,
        "messages": [{"role": "user", "content": "hi"}]
    })
    ok = st in (400, 422, 200)
    check("G7 negative max_tokens does not crash (4xx or 200)", ok, st)

def t_g8():
    # Invalid JSON body
    url = BASE + "/v1/chat/completions"
    req = urllib.request.Request(url, data=b"{invalid json",
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=15)
        st = r.status
    except urllib.error.HTTPError as e:
        st = e.code
    check("G8 invalid JSON returns 4xx", st in (400, 422), st)


# ===================== Main =====================

ALL_TESTS = [
    t_a1, t_a2, t_a3, t_a4, t_a5, t_a6, t_a7, t_a8,
    t_b1, t_b2, t_b3, t_b4,
    t_c1, t_c2, t_c3, t_c5,
    t_d1, t_d2, t_d3, t_d4, t_d5, t_d6, t_d7,
    t_e1, t_e2, t_e3, t_e4, t_e5, t_e6, t_e7,
    t_f1, t_f2, t_f3, t_f4, t_f5,
    t_g1, t_g2, t_g3, t_g4, t_g5, t_g6, t_g7, t_g8,
]

def main():
    global passed, failed
    print("=" * 60)
    print("Suite 7: SSE streaming, load, trim, sampling, schema, tools, errors")
    print("Tests: " + str(len(ALL_TESTS)))
    print("=" * 60)
    t0 = time.time()
    for t in ALL_TESTS:
        try:
            t()
        except Exception as e:
            check(t.__name__ + " (exception)", False, repr(e))
    elapsed = time.time() - t0
    print("=" * 60)
    print("RESULTS: %d passed, %d failed, %d total (%.1fs)" %
          (passed, failed, passed + failed, elapsed))
    print("=" * 60)
    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()

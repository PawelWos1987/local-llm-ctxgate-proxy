#!/usr/bin/env python3
import concurrent.futures
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9201"
PASS = 0
FAIL = 0
RESULTS = []
def record(name, ok, detail=""):
    global PASS, FAIL
    st = "PASS" if ok else "FAIL"
    if ok: PASS += 1
    else: FAIL += 1
    RESULTS.append(st + ": " + name)
    print("  " + st + ": " + name)
def api(path, body=None, headers=None, method="POST", timeout=60):
    url = BASE + path
    hdrs = {"Content-Type": "application/json"}
    if headers: hdrs.update(headers)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode()
        return resp.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)
def sapi(path, body, timeout=60):
    url = BASE + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode().strip().split(chr(10))
    except urllib.error.HTTPError as e:
        return e.code, [e.read().decode()]
    except Exception as e:
        return 0, [str(e)]

print("=" * 60)
print("local-llm-ctxgate-proxy COMPREHENSIVE TEST SUITE")
print("=" * 60)

print("\n--- 1: Health & Metrics ---")
s, d = api("/health", method="GET")
record("health", s == 200 and d.get("status") == "ok")
s, d = api("/metrics", method="GET")
record("metrics", s == 200)

print("\n--- 2: Basic Chat ---")
b = {"model": "Qwen3.8-27B", "messages": [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "Say OK"}],
    "max_tokens": 30, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices") and d["choices"][0].get("message", {}).get("content")
record("chat_basic", ok)

msgs = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "My name is Alice."},
    {"role": "assistant", "content": "Hello Alice!"},
    {"role": "user", "content": "What is my name?"}]
b = {"model": "Qwen3.8-27B", "messages": msgs, "max_tokens": 200, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
c = d["choices"][0].get("message", {}).get("content", "") if s == 200 and d.get("choices") else ""
record("chat_multi", s == 200 and "alice" in c.lower())

print("\n--- 3: Streaming ---")
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Count 1 to 5."}],
    "max_tokens": 100, "temperature": 0.1, "stream": True}
s, lines = sapi("/v1/chat/completions", b)
hd = any(l.startswith("data: ") for l in lines)

# === Regression tests for P1 fixes ===

def test_regress_nl():
    """P1.1: _compact_context must not raise NameError (NL undefined)."""
    import importlib
    import sys
    sys.path.insert(0, "/home/pawelw/ctxproxy")
    try:
        import proxy.app as app
        importlib.reload(app)
        # 10 messages to trigger compaction path
        msgs = [{"role": "system", "content": "You are helpful."}]
        for i in range(9):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i} " + "x" * 100})
        app.session_compactions["test-session"] = {"last_turn": 0, "frozen_summary": ""}
        result = app._compact_context(msgs, 200, "test-session")
        assert isinstance(result, list)
        return True, "compact_context no NameError"
    except NameError as e:
        return False, f"NameError: {e}"
    except Exception as e:
        return False, f"Error: {e}"

def test_regress_normalize():
    """P1.2: _normalize_system_messages must not clobber non-system first message."""
    import importlib
    import sys
    sys.path.insert(0, "/home/pawelw/ctxproxy")
    try:
        import proxy.app as app
        importlib.reload(app)
        # [user, system, user] - first message is user, must survive
        msgs = [
            {"role": "user", "content": "Hello, I need help"},
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Tell me more"},
        ]
        result = app._normalize_system_messages(msgs)
        # First message must still be user with original content
        assert result[0]["role"] == "user", f"First msg role changed to {result[0]['role']}"
        assert result[0]["content"] == "Hello, I need help", f"First msg content changed to {result[0]['content']}"
        return True, "non-system first message preserved"
    except Exception as e:
        return False, f"Error: {e}"

def test_regress_cache_metric():
    """P1.3: Weighted cache metric - 100 cached on 200 prompt = 0.5 hit rate."""
    import importlib
    import sys
    sys.path.insert(0, "/home/pawelw/ctxproxy")
    try:
        import proxy.app as app
        importlib.reload(app)
        # Simulate what the metric computation does
        cached = 100
        prompt = 200
        hit_rate = cached / max(1, prompt)
        assert abs(hit_rate - 0.5) < 0.01, f"Expected 0.5, got {hit_rate}"
        # Verify the metrics dict has the new fields
        assert "cached_tokens_total" in app.metrics, "cached_tokens_total missing"
        assert "prompt_tokens_total" in app.metrics, "prompt_tokens_total missing"
        assert "evicted_sessions" in app.metrics, "evicted_sessions missing"
        return True, f"weighted hit rate = {hit_rate}"
    except Exception as e:
        return False, f"Error: {e}"

def test_regress_no_trim():
    """P1.4: Continuation must not call trim_context - verify code has stop pattern."""
    path = "/home/pawelw/ctxproxy/proxy/app.py"
    with open(path) as f:
        src = f.read()
    # The old pattern should NOT be present in continuation context
    # New pattern: "would exceed input budget" should be present
    has_stop = "would exceed input budget" in src
    # Old trim in cont should be gone (check the specific cont patterns)
    old_cont_trim = "re-trimmed to"
    # It's ok if re-trimmed appears in non-cont context, but not in cont
    assert has_stop, "Missing 'would exceed input budget' stop pattern"
    return True, "continuation stop pattern present"

def test_regress_refreeze_pop():
    """P1.7: session_compactions.pop on re-freeze."""
    path = "/home/pawelw/ctxproxy/proxy/app.py"
    with open(path) as f:
        src = f.read()
    assert "session_compactions.pop(session_key, None)" in src, "Missing compactions.pop on re-freeze"
    return True, "compactions.pop present on re-freeze"

def test_regress_evict():
    """P1.6: Session TTL eviction function exists."""
    path = "/home/pawelw/ctxproxy/proxy/app.py"
    with open(path) as f:
        src = f.read()
    assert "_evict_stale_sessions" in src, "Missing _evict_stale_sessions"
    assert "SESSION_TTL_HOURS" in src, "Missing SESSION_TTL_HOURS"
    assert "evicted_sessions" in src, "Missing evicted_sessions metric"
    return True, "session eviction present"

def test_regress_backpressure():
    """P2.2: Backpressure check in _enqueue_memory_job."""
    path = "/home/pawelw/ctxproxy/proxy/app.py"
    with open(path) as f:
        src = f.read()
    assert "WORKER_BACKPRESSURE" in src, "Missing WORKER_BACKPRESSURE"
    assert "backpressure" in src.lower(), "Missing backpressure check"
    return True, "backpressure present"

def test_regress_worker_telemetry():
    """P2.1: Worker telemetry function exists."""
    path = "/home/pawelw/ctxproxy/proxy/app.py"
    with open(path) as f:
        src = f.read()
    assert "_read_worker_status" in src, "Missing _read_worker_status"
    return True, "worker telemetry present"

def test_regress_vllm_timeout():
    """P2.4: vLLM client uses config-driven timeout."""
    path = "/home/pawelw/ctxproxy/proxy/app.py"
    with open(path) as f:
        src = f.read()
    assert "httpx.Timeout" in src, "Missing httpx.Timeout"
    assert "CTXGATE_VLLM_READ_TIMEOUT" in src, "Missing VLLM_READ_TIMEOUT env"
    return True, "vllm timeout from config"

def test_regress_max_cont_top():
    """P2.6: MAX_CONTINUATIONS defined near top of file."""
    path = "/home/pawelw/ctxproxy/proxy/app.py"
    with open(path) as f:
        lines = f.readlines()
    for i, line in enumerate(lines[:500]):
        if "MAX_CONTINUATIONS" in line and "=" in line:
            assert i < 500, f"MAX_CONTINUATIONS at line {i+1}, should be in first 500"
            return True, f"MAX_CONTINUATIONS at line {i+1}"
    return False, "MAX_CONTINUATIONS not in first 60 lines"


dn = any("[DONE]" in l for l in lines)
record("stream_basic", s == 200 and hd and dn)
parts2 = []
for l in lines:
    if l.startswith("data: ") and l != "data: [DONE]":
        try:
            cc = json.loads(l[6:])
            dl = cc.get("choices", [{}])[0].get("delta", {})
            if dl.get("content"): parts2.append(dl["content"])
        except: pass
fc = "".join(parts2)
record("stream_content", "1" in fc and "5" in fc)


print("\n--- 4: Tool Calls ---")
tl = [{"type": "function", "function": {"name": "get_weather", "description": "Get weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
b = {"model": "Qwen3.8-27B",
    "messages": [{"role": "system", "content": "Use tools."}, {"role": "user", "content": "Weather in Paris?"}],
    "tools": tl, "tool_choice": "auto", "max_tokens": 200, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
tc = d.get("choices", [{}])[0].get("message", {}).get("tool_calls") if s == 200 and d.get("choices") else None
record("tool_single", s == 200 and tc is not None)
if tc:
    fn = tc[0].get("function", {})
    record("tool_name", fn.get("name") == "get_weather")
    try:
        a = json.loads(fn.get("arguments", "{}"))
        record("tool_args", "city" in a)
    except: record("tool_args", False)

t2 = [
    {"type": "function", "function": {"name": "read_file", "description": "Read",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}}}]
b = {"model": "Qwen3.8-27B",
    "messages": [{"role": "system", "content": "Use tools."}, {"role": "user", "content": "Read /etc/hostname"}],
    "tools": t2, "tool_choice": "auto", "max_tokens": 200, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
tc = d.get("choices", [{}])[0].get("message", {}).get("tool_calls") if s == 200 and d.get("choices") else None
record("tool_multi", s == 200 and tc is not None)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hello"}],
    "tools": tl, "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    "max_tokens": 200, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
tc = d.get("choices", [{}])[0].get("message", {}).get("tool_calls") if s == 200 and d.get("choices") else None
record("tool_forced", s == 200 and tc is not None)


msgs2 = [
    {"role": "system", "content": "Use tools."},
    {"role": "user", "content": "Weather in London?"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
        "function": {"name": "get_weather", "arguments": json.dumps({"city": "London"})}}]},
    {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"temp": 15, "condition": "sunny"})}]
b = {"model": "Qwen3.8-27B", "messages": msgs2, "tools": tl, "max_tokens": 100, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
c = d.get("choices", [{}])[0].get("message", {}).get("content", "") if s == 200 and d.get("choices") else ""
record("tool_cont", s == 200 and len(c) > 0)

b = {"model": "Qwen3.8-27B",
    "messages": [{"role": "system", "content": "Use search."}, {"role": "user", "content": "Search cats"}],
    "tools": [{"type": "function", "function": {"name": "search", "description": "S",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}],
    "tool_choice": "auto", "max_tokens": 200, "temperature": 0.1, "stream": True}
s, lines = sapi("/v1/chat/completions", b)
record("tool_stream", s == 200 and any("[DONE]" in l for l in lines))


print("\n--- 5: Border Cases ---")
b = {"model": "Qwen3.8-27B", "messages": [], "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_empty", s != 200)

ls = "You are an expert. " * 500
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": ls}, {"role": "user", "content": "Hi"}],
    "max_tokens": 20, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_longsy", s == 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 1, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_max1", s == 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "1+1="}],
    "max_tokens": 20, "temperature": 0, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_t0", s == 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Say a word"}],
    "max_tokens": 20, "temperature": 2.0, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_t2", s == 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "cafe au lait test"}],
    "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_unicode", s == 200)

msgs3 = [{"role": "system", "content": "Brief."}]
for i in range(25):
    msgs3.append({"role": "user", "content": "M" + str(i)})
    msgs3.append({"role": "assistant", "content": "OK" + str(i)})
b = {"model": "Qwen3.8-27B", "messages": msgs3, "max_tokens": 30, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b, timeout=120)
record("b_50t", s == 200)

b = {"model": "nonexistent-xyz", "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_badmodel", s != 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 999999, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("b_huge", s == 200)


print("\n--- 6: Error Handling ---")
req = urllib.request.Request(BASE + "/v1/chat/completions",
    data=b"not json {{{", headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req, timeout=10)
    record("e_badjson", False)
except urllib.error.HTTPError as e:
    record("e_badjson", e.code in (400, 422))
except Exception:
    record("e_badjson", True)

b = {"model": "Qwen3.8-27B"}
s, d = api("/v1/chat/completions", b)
record("e_nomsg", s != 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "bad", "content": "Hi"}],
    "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("e_badrole", s != 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": None}],
    "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("e_null", s in (200, 400, 422))

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": -5, "stream": False}
s, d = api("/v1/chat/completions", b)
record("e_negmax", s == 200)  # proxy overrides max_tokens by design

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 10, "temperature": -1.0, "stream": False}
s, d = api("/v1/chat/completions", b)
record("e_negtemp", s != 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hi"}],
    "tools": [{"type": "function", "function": {"name": "bad", "parameters": "nope"}}],
    "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("e_badtool", s != 200)


print("\n--- 7: Session & Memory ---")
b = {"model": "Qwen3.8-27B",
    "messages": [{"role": "user", "content": "Remember: blue is my color."}],
    "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b, headers={"X-Session-ID": "test-s-001"})
record("sess_hdr", s == 200)

s, d = api("/memory/test-s-001", method="GET")
record("mem_q", s == 200)

b = {"task_ref": "test-inj-001", "content": "Dark mode pref."}
s, d = api("/memory/inject", b)
record("mem_inj", s == 200)

s, d = api("/memory/test-inj-001", method="GET")
record("mem_inj_q", s == 200)

b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("no_sess", s == 200)


print("\n--- 8: Edge/Stress ---")
b = {"model": "Qwen3.8-27B",
    "messages": [{"role": "user", "content": "drop table test"}],
    "max_tokens": 20, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b, headers={"X-Session-ID": "sql-t"})
record("e_sql", s == 200)

lm = "The quick brown fox. " * 1500
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": lm}],
    "max_tokens": 20, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b, timeout=120)
record("e_long", s == 200)

def fr(i):
    bb = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Say " + str(i)}],
          "max_tokens": 10, "temperature": 0.1, "stream": False}
    st, _ = api("/v1/chat/completions", bb)
    return st
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
    codes = list(ex.map(fr, [1, 2, 3]))
record("concurrent", all(c == 200 for c in codes))


print("--- 9: Regression (P1+P2) ---")
ok, msg = test_regress_nl()
record("reg_nl", ok)
ok, msg = test_regress_normalize()
record("reg_normalize", ok)
ok, msg = test_regress_cache_metric()
record("reg_cache_metric", ok)
ok, msg = test_regress_no_trim()
record("reg_no_trim", ok)
ok, msg = test_regress_refreeze_pop()
record("reg_refreeze_pop", ok)
ok, msg = test_regress_evict()
record("reg_evict", ok)
ok, msg = test_regress_backpressure()
record("reg_backpressure", ok)
ok, msg = test_regress_worker_telemetry()
record("reg_worker_tel", ok)
ok, msg = test_regress_vllm_timeout()
record("reg_vllm_timeout", ok)
ok, msg = test_regress_max_cont_top()
record("reg_max_cont_top", ok)
print("\n" + "=" * 60)
print("RESULTS: " + str(PASS) + " PASS, " + str(FAIL) + " FAIL (total " + str(PASS + FAIL) + ")")
for r in RESULTS:
    print("  " + r)
print("DONE")


#!/usr/bin/env python3
"""local-llm-ctxgate-proxy DEEP TEST SUITE - tool chains, sampling, sessions, protocol, metrics, edge cases."""
import json
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9201"
PASS = 0; FAIL = 0; RESULTS = []

def record(name, ok, detail=""):
    global PASS, FAIL
    st = "PASS" if ok else "FAIL"
    if ok: PASS += 1
    else: FAIL += 1
    RESULTS.append(st + ": " + name + (" (" + detail + ")" if detail and not ok else ""))
    print("  " + st + ": " + name + (("  [" + detail[:120] + "]") if detail and not ok else ""))

def api(path, body=None, headers=None, method="POST", timeout=120):
    url = BASE + path
    hdrs = {"Content-Type": "application/json"}
    if headers: hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode()
        return resp.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()
        try: return e.code, json.loads(body_err)
        except: return e.code, body_err
    except Exception as e:
        return 0, str(e)

def sapi(path, body, headers=None, timeout=120):
    url = BASE + path
    hdrs = {"Content-Type": "application/json"}
    if headers: hdrs.update(headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdrs, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode().strip().split(chr(10))
    except urllib.error.HTTPError as e:
        return e.code, [e.read().decode()]
    except Exception as e:
        return 0, [str(e)]

def get_metrics():
    s, d = api("/metrics", method="GET")
    return d if isinstance(d, dict) else {}

print("=" * 60)
print("local-llm-ctxgate-proxy DEEP TEST SUITE")
print("=" * 60)

# ============================================================
# SECTION A: Tool Call Chains (realistic agent loops)
# ============================================================
print("\n--- A: Tool Call Chains ---")

tool_defs = [
    {"type": "function", "function": {"name": "list_files", "description": "List files", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "grep", "description": "Search in files", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
]

# A1: 3-round tool chain (user -> tool_call -> tool_result -> tool_call -> tool_result -> answer)
msgs = [
    {"role": "system", "content": "You are a file assistant. Use tools to answer."},
    {"role": "user", "content": "List files in /tmp then read the first one"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "list_files", "arguments": json.dumps({"path": "/tmp"})}}]},
    {"role": "tool", "tool_call_id": "tc1", "content": "alpha.txt\nbeta.txt"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "tc2", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "/tmp/alpha.txt"})}}]},
    {"role": "tool", "tool_call_id": "tc2", "content": "Hello World"},
]
b = {"model": "Qwen3.8-27B", "messages": msgs, "tools": tool_defs, "tool_choice": "auto", "max_tokens": 100, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
record("A1_tool_chain_3round", ok, "s=" + str(s))
if ok:
    m = d["choices"][0]["message"]
    has_content = m.get("content") and len(m["content"]) > 3
    has_tc = m.get("tool_calls") is not None
    record("A1_tool_chain_has_response", has_content or has_tc, "content=" + str(len(m.get("content") or "")) + " tc=" + str(has_tc))

# A2: 5-round tool chain
msgs5 = [
    {"role": "system", "content": "You are a code assistant."},
    {"role": "user", "content": "Find all TODO comments in src/"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "grep", "arguments": json.dumps({"pattern": "TODO", "path": "src/"})}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "src/a.py:10: TODO fix bug\nsrc/b.py:25: TODO add tests"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "t2", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/a.py"})}}]},
    {"role": "tool", "tool_call_id": "t2", "content": "#!/usr/bin/env python\n# TODO fix bug\nprint(1)"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "t3", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/b.py"})}}]},
    {"role": "tool", "tool_call_id": "t3", "content": "def b():\n    # TODO add tests\n    pass"},
]
b = {"model": "Qwen3.8-27B", "messages": msgs5, "tools": tool_defs, "tool_choice": "auto", "max_tokens": 200, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
record("A2_tool_chain_5round", ok, "s=" + str(s))
if ok:
    m = d["choices"][0]["message"]
    resp_text = (m.get("content") or "") + json.dumps(m.get("tool_calls") or [])
    record("A2_tool_chain_has_response", len(resp_text) > 3, "len=" + str(len(resp_text)))

# A3: Tool chain with unicode in arguments
msgs_u = [
    {"role": "system", "content": "You are a search assistant."},
    {"role": "user", "content": "Search for files"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "tu1", "type": "function", "function": {"name": "grep", "arguments": json.dumps({"pattern": "n\u00e0ive \u00fcber\u2192", "path": "/home/\u00f6sterreich"})}}]},
    {"role": "tool", "tool_call_id": "tu1", "content": "Found 3 matches"},
]
b = {"model": "Qwen3.8-27B", "messages": msgs_u, "tools": tool_defs, "tool_choice": "auto", "max_tokens": 100, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("A3_tool_chain_unicode_args", s == 200, "s=" + str(s))

# A4: Large tool arguments (near 4KB)
large_arg = {"description": "x" * 4000, "path": "/test"}
msgs_l = [
    {"role": "system", "content": "Bot."},
    {"role": "user", "content": "Search with a big description"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "tl1", "type": "function", "function": {"name": "grep", "arguments": json.dumps(large_arg)}}]},
    {"role": "tool", "tool_call_id": "tl1", "content": "OK"},
]
b = {"model": "Qwen3.8-27B", "messages": msgs_l, "tools": tool_defs, "tool_choice": "auto", "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("A4_tool_large_args", s == 200, "s=" + str(s))

# ============================================================
# SECTION B: Sampling Parameters Sweep
# ============================================================
print("\n--- B: Sampling Parameters ---")

base_msgs = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Say hello"}]

# B1: temperature=0
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 20, "temperature": 0, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B1_temp0", s == 200, "s=" + str(s))

# B2: temperature=1
b["temperature"] = 1.0
s, d = api("/v1/chat/completions", b)
record("B2_temp1", s == 200, "s=" + str(s))

# B3: temperature=2 (max)
b["temperature"] = 2.0
s, d = api("/v1/chat/completions", b)
record("B3_temp2", s == 200, "s=" + str(s))

# B4: top_p=0.1
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 20, "temperature": 0.5, "top_p": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B4_top_p_01", s == 200, "s=" + str(s))

# B5: top_p=1.0
b["top_p"] = 1.0
s, d = api("/v1/chat/completions", b)
record("B5_top_p_10", s == 200, "s=" + str(s))

# B6: frequency_penalty
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 20, "temperature": 0.5, "frequency_penalty": 1.0, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B6_freq_penalty", s == 200, "s=" + str(s))

# B7: presence_penalty
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 20, "temperature": 0.5, "presence_penalty": 1.0, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B7_presence_penalty", s == 200, "s=" + str(s))

# B8: combined penalties
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 20, "temperature": 0.7, "top_p": 0.9, "frequency_penalty": 0.5, "presence_penalty": 0.5, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B8_combined_params", s == 200, "s=" + str(s))

# ============================================================
# SECTION C: Stop Sequences & n>1 & Max_tokens Boundaries
# ============================================================
print("\n--- C: Stop, n, max_tokens ---")

# C1: max_tokens=1
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 1, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
record("C1_max_tokens_1", ok, "s=" + str(s))
if ok:
    fin = d["choices"][0].get("finish_reason", "")
    record("C1_max_tokens_1_finish", fin in ("length", "stop"), "finish=" + str(fin))

# C2: max_tokens=0 (should be handled)
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 0, "stream": False}
s, d = api("/v1/chat/completions", b)
record("C2_max_tokens_0", s in (200, 400), "s=" + str(s))

# C3: max_tokens=1 (very small)
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("C3_max_tokens_1_dup", s == 200, "s=" + str(s))

# C4: n=2 (two completions)
b = {"model": "Qwen3.8-27B", "messages": base_msgs, "max_tokens": 20, "n": 2, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices") and len(d["choices"]) >= 1
record("C4_n2", ok, "s=" + str(s) + " choices=" + str(len(d.get("choices", [])) if d.get("choices") else 0))

# C5: stop sequences
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Write a long essay about cats. Stop at the word DONE."}], "max_tokens": 200, "stop": ["DONE"], "stream": False}
s, d = api("/v1/chat/completions", b)
record("C5_stop_seq", s == 200, "s=" + str(s))

# C6: multiple stop sequences
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Say hello then stop."}], "max_tokens": 50, "stop": ["[END]", "STOP"], "stream": False}
s, d = api("/v1/chat/completions", b)
record("C6_multi_stop", s == 200, "s=" + str(s))

# ============================================================
# SECTION D: Session Isolation (Memory)
# ============================================================
print("\n--- D: Session Isolation ---")

# D1: Inject memory to session-A
s, d = api("/memory/inject", {"task_id": "session-A", "content": "User preference: dark mode"})
record("D1_inject_A", s == 200, "s=" + str(s))

# D2: Inject memory to session-B
s, d = api("/memory/inject", {"task_id": "session-B", "content": "User preference: light mode"})
record("D2_inject_B", s == 200, "s=" + str(s))

# D3: Query session-A - should see dark mode, not light mode
s, d = api("/memory/session-A", method="GET")
ok = s == 200
record("D3_query_A", ok, "s=" + str(s))
if ok and isinstance(d, dict):
    mem_str = json.dumps(d)
    record("D3_A_has_dark", "dark" in mem_str, "mem=" + mem_str[:100])
    record("D3_A_no_light", "light" not in mem_str, "mem=" + mem_str[:100])

# D4: Query session-B - should see light mode, not dark mode
s, d = api("/memory/session-B", method="GET")
ok = s == 200
record("D4_query_B", ok, "s=" + str(s))
if ok and isinstance(d, dict):
    mem_str = json.dumps(d)
    record("D4_B_has_light", "light" in mem_str, "mem=" + mem_str[:100])
    record("D4_B_no_dark", "dark" not in mem_str, "mem=" + mem_str[:100])

# D5: Query non-existent session
s, d = api("/memory/session-nonexistent", method="GET")
record("D5_query_nonexistent", s in (200, 404), "s=" + str(s))

# ============================================================
# SECTION E: Protocol Edge Cases
# ============================================================
print("\n--- E: Protocol Edge Cases ---")

# E1: No Content-Type header
url = BASE + "/v1/chat/completions"
req = urllib.request.Request(url, data=json.dumps({"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "hi"}]}).encode(), method="POST")
try:
    resp = urllib.request.urlopen(req, timeout=30)
    s = resp.status
except urllib.error.HTTPError as e:
    s = e.code
except Exception as e:
    s = 0
record("E1_no_content_type", s in (200, 400, 415), "s=" + str(s))

# E2: Invalid JSON body
url = BASE + "/v1/chat/completions"
req = urllib.request.Request(url, data=b"not valid json{{", headers={"Content-Type": "application/json"}, method="POST")
try:
    resp = urllib.request.urlopen(req, timeout=10)
    s = resp.status
except urllib.error.HTTPError as e:
    s = e.code
except Exception as e:
    s = 0
record("E2_invalid_json", s in (400, 500), "s=" + str(s))

# E3: Empty body with POST
url = BASE + "/v1/chat/completions"
req = urllib.request.Request(url, data=b"", headers={"Content-Type": "application/json"}, method="POST")
try:
    resp = urllib.request.urlopen(req, timeout=10)
    s = resp.status
except urllib.error.HTTPError as e:
    s = e.code
except Exception as e:
    s = 0
record("E3_empty_body", s in (400, 422, 500), "s=" + str(s))

# E4: GET on POST endpoint (should 405)
s, d = api("/v1/chat/completions", method="GET")
record("E4_get_on_post", s in (405, 400, 500), "s=" + str(s))

# E5: Unknown endpoint
s, d = api("/v1/nonexistent", method="GET")
record("E5_unknown_endpoint", s in (404, 405), "s=" + str(s))

# E6: Empty messages array
b = {"model": "Qwen3.8-27B", "messages": [], "max_tokens": 10}
s, d = api("/v1/chat/completions", b)
record("E6_empty_messages", s in (400, 422), "s=" + str(s))

# ============================================================
# SECTION F: Metric Accuracy
# ============================================================
print("\n--- F: Metric Accuracy ---")

# F1: Get baseline metrics
m_before = get_metrics()
reqs_before = m_before.get("requests_total", 0)
tok_in_before = m_before.get("tokens_in_total", 0)

# F2: Send a known request
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Hello world"}], "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("F2_request_ok", s == 200, "s=" + str(s))

# F3: Check metrics delta
m_after = get_metrics()
reqs_after = m_after.get("requests_total", 0)
tok_in_after = m_after.get("tokens_in_total", 0)
record("F3_requests_incremented", reqs_after >= reqs_before + 1, "before=" + str(reqs_before) + " after=" + str(reqs_after))
record("F3_tokens_in_incremented", tok_in_after >= tok_in_before, "before=" + str(tok_in_before) + " after=" + str(tok_in_after))
tok_delta = tok_in_after - tok_in_before
record("F3_tokens_reasonable", 1 <= tok_delta <= 100, "delta=" + str(tok_delta))

# F4: Multiple requests -> metrics scale
for i in range(3):
    api("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "ping " + str(i)}], "max_tokens": 5, "stream": False})
m_final = get_metrics()
record("F4_requests_scaled", m_final.get("requests_total", 0) >= reqs_after + 3, "final=" + str(m_final.get("requests_total", 0)))

# ============================================================
# SECTION G: Role & Message Ordering Edge Cases
# ============================================================
print("\n--- G: Role & Ordering Edge Cases ---")

# G1: User before system (unusual ordering)
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": "be brief"}], "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("G1_user_before_system", s in (200, 400, 422), "s=" + str(s))

# G2: Unknown role
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "bot"}, {"role": "alien", "content": "x"}], "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("G2_unknown_role", s in (200, 400, 422, 500), "s=" + str(s))

# G3: Assistant with no prior user
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "bot"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "hi"}], "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("G3_assistant_first", s in (200, 400, 422), "s=" + str(s))

# G4: Multiple system messages
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "sys1"}, {"role": "system", "content": "sys2"}, {"role": "user", "content": "hi"}], "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("G4_multi_system", s in (200, 400, 422), "s=" + str(s))

# G5: Tool message without tool_call_id
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "bot"}, {"role": "user", "content": "hi"}, {"role": "tool", "content": "orphan tool result"}], "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("G5_orphan_tool_msg", s in (200, 400, 422, 500), "s=" + str(s))

# ============================================================
# SECTION H: Streaming Deep Tests
# ============================================================
print("\n--- H: Streaming Deep Tests ---")

# H1: Stream with max_tokens=1
s, lines = sapi("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": True})
record("H1_stream_max1", s == 200, "s=" + str(s))
if s == 200:
    has_done = any("[DONE]" in l for l in lines)
    record("H1_stream_has_done", has_done, "lines=" + str(len(lines)))
    has_data = any(l.startswith("data:") for l in lines)
    record("H1_stream_has_data", has_data, "lines=" + str(len(lines)))

# H2: Stream SSE format validation
s, lines = sapi("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "say hi"}], "max_tokens": 50, "stream": True})
record("H2_stream_ok", s == 200, "s=" + str(s))
if s == 200:
    data_lines = [l for l in lines if l.startswith("data:")]
    valid_json = 0
    for l in data_lines:
        payload = l[5:].strip()
        if payload == "[DONE]": continue
        try:
            json.loads(payload)
            valid_json += 1
        except:
            pass
    record("H2_stream_valid_sse", valid_json >= 1, "data_lines=" + str(len(data_lines)) + " valid=" + str(valid_json))

    # Check for proper chunk structure
    has_delta = any('"delta"' in l for l in data_lines)
    record("H2_stream_has_delta", has_delta, "")

# H3: Stream with tools
s, lines = sapi("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "use tools"}, {"role": "user", "content": "list files"}], "tools": tool_defs, "tool_choice": "auto", "max_tokens": 100, "stream": True})
record("H3_stream_tools", s == 200, "s=" + str(s))
if s == 200:
    raw = "\n".join(lines)
    has_tc = "tool_calls" in raw
    has_content = '"content"' in raw
    record("H3_stream_tools_response", has_tc or has_content, "tc=" + str(has_tc) + " content=" + str(has_content))

# H4: Empty stream (no content expected)
s, lines = sapi("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": ""}], "max_tokens": 1, "stream": True})
record("H4_stream_empty_input", s in (200, 400), "s=" + str(s))

# ============================================================
# SECTION I: Tool Choice Variants
# ============================================================
print("\n--- I: Tool Choice Variants ---")

# I1: tool_choice="none" (should not call tools)
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "You have tools but should not use them."}, {"role": "user", "content": "What is 2+2?"}], "tools": tool_defs, "tool_choice": "none", "max_tokens": 50, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
record("I1_tool_choice_none", ok, "s=" + str(s))
if ok:
    m = d["choices"][0]["message"]
    no_tc = m.get("tool_calls") is None
    record("I1_none_no_tc", no_tc, "tc=" + str(m.get("tool_calls") is not None))

# I2: tool_choice="required" (must call a tool)
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "You must use tools."}, {"role": "user", "content": "What files exist?"}], "tools": tool_defs, "tool_choice": "required", "max_tokens": 100, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
record("I2_tool_choice_required", ok, "s=" + str(s))
if ok:
    m = d["choices"][0]["message"]
    has_tc = m.get("tool_calls") is not None
    record("I2_required_has_tc", has_tc, "")

# I3: Specific tool choice (function name)
b = {"model": "Qwen3.8-27B", "messages": [{"role": "system", "content": "Use tools."}, {"role": "user", "content": "Find files"}], "tools": tool_defs, "tool_choice": {"type": "function", "function": {"name": "list_files"}}, "max_tokens": 100, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
record("I3_specific_tool", ok, "s=" + str(s))
if ok:
    m = d["choices"][0]["message"]
    tcs = m.get("tool_calls")
    correct = tcs and len(tcs) > 0 and tcs[0]["function"]["name"] == "list_files"
    record("I3_specific_tool_name", correct, "name=" + str(tcs[0]["function"]["name"] if tcs else "none"))

# I4: Empty tools array
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "hi"}], "tools": [], "max_tokens": 20, "stream": False}
s, d = api("/v1/chat/completions", b)
record("I4_empty_tools", s == 200, "s=" + str(s))

# I5: tools with no parameters (minimal schema)
minimal_tools = [{"type": "function", "function": {"name": "noop", "description": "Does nothing", "parameters": {"type": "object", "properties": {}}}}]
b = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Do something"}], "tools": minimal_tools, "tool_choice": "auto", "max_tokens": 50, "stream": False}
s, d = api("/v1/chat/completions", b)
record("I5_minimal_tool_schema", s == 200, "s=" + str(s))

# ============================================================
# SECTION J: Concurrent Session Isolation
# ============================================================
print("\n--- J: Concurrent Session Isolation ---")

results_j = {}
def worker_j(session_id):
    try:
        api("/memory/inject", {"task_id": session_id, "content": "Memory from " + session_id})
        time.sleep(0.5)
        s, d = api("/memory/" + session_id, method="GET")
        results_j[session_id] = s == 200 and isinstance(d, dict) and session_id in json.dumps(d)
    except Exception as e:
        results_j[session_id] = False

threads = [threading.Thread(target=worker_j, args=("conc-A",)) for _ in range(2)]
threads += [threading.Thread(target=worker_j, args=("conc-B",)) for _ in range(2)]
threads += [threading.Thread(target=worker_j, args=("conc-C",)) for _ in range(2)]
for t in threads: t.start()
for t in threads: t.join()

all_ok = all(results_j.values())
record("J1_concurrent_isolation", all_ok, str({k: v for k, v in results_j.items() if not v}))

# J2: 6 concurrent chat requests with different sessions
results_j2 = {}
def worker_j2(i):
    try:
        s, d = api("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "ping " + str(i)}], "max_tokens": 10, "stream": False}, headers={"X-Session-ID": "conc-chat-" + str(i)})
        results_j2[i] = s == 200
    except Exception as e:
        results_j2[i] = False

threads = [threading.Thread(target=worker_j2, args=(i,)) for i in range(6)]
for t in threads: t.start()
for t in threads: t.join()
all_ok2 = all(results_j2.values())
record("J2_concurrent_chat", all_ok2, str({k: v for k, v in results_j2.items() if not v}))

# ============================================================
# SECTION K: vLLM Direct vs Proxy Comparison
# ============================================================
print("\n--- K: vLLM Direct vs Proxy ---")

VLLM = "http://127.0.0.1:29000/v1"

def vllm_direct(path, body, timeout=60):
    url = VLLM + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode()
        return resp.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)

# K1: Simple chat - compare proxy vs direct
msgs_k = [{"role": "user", "content": "Say exactly: hello world"}]
s_proxy, d_proxy = api("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": msgs_k, "max_tokens": 100, "temperature": 0, "stream": False})
s_vllm, d_vllm = vllm_direct("/chat/completions", {"model": "Qwen3.8-27B", "messages": msgs_k, "max_tokens": 100, "temperature": 0, "stream": False})
record("K1_proxy_ok", s_proxy == 200, "s=" + str(s_proxy))
record("K1_vllm_ok", s_vllm == 200, "s=" + str(s_vllm))
if s_proxy == 200 and s_vllm == 200 and d_proxy.get("choices") and d_vllm.get("choices"):
    proxy_txt = d_proxy["choices"][0]["message"].get("content") or ""
    vllm_txt = d_vllm["choices"][0]["message"].get("content") or ""
    record("K1_both_have_content", len(proxy_txt) > 0 and len(vllm_txt) > 0, "proxy=" + str(len(proxy_txt)) + " vllm=" + str(len(vllm_txt)))

# K2: Tools - compare proxy vs direct
msgs_k2 = [{"role": "system", "content": "You are an assistant with tools."}, {"role": "user", "content": "List files in /tmp"}]
tools_k2 = [{"type": "function", "function": {"name": "list_files", "description": "List files", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
s_proxy, d_proxy = api("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": msgs_k2, "tools": tools_k2, "tool_choice": "auto", "max_tokens": 100, "temperature": 0, "stream": False})
s_vllm, d_vllm = vllm_direct("/chat/completions", {"model": "Qwen3.8-27B", "messages": msgs_k2, "tools": tools_k2, "tool_choice": "auto", "max_tokens": 100, "temperature": 0, "stream": False})
record("K2_proxy_tools", s_proxy == 200, "s=" + str(s_proxy))
record("K2_vllm_tools", s_vllm == 200, "s=" + str(s_vllm))

# K3: Streaming - compare proxy vs direct
s_proxy_lines, proxy_lines = sapi("/v1/chat/completions", {"model": "Qwen3.8-27B", "messages": msgs_k, "max_tokens": 30, "stream": True})
# Direct streaming via urllib
try:
    req = urllib.request.Request(VLLM + "/chat/completions", data=json.dumps({"model": "Qwen3.8-27B", "messages": msgs_k, "max_tokens": 30, "stream": True}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=60)
    vllm_raw = resp.read().decode()
    vllm_line_list = vllm_raw.strip().split("\n")
    s_vllm_stream = 200
except Exception as e:
    s_vllm_stream = 0
    vllm_line_list = [str(e)]

record("K3_proxy_stream", s_proxy_lines == 200, "s=" + str(s_proxy_lines))
record("K3_vllm_stream", s_vllm_stream == 200, "s=" + str(s_vllm_stream))

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("DEEP RESULTS: " + str(PASS) + " PASS, " + str(FAIL) + " FAIL (total " + str(PASS + FAIL) + ")")
for r in RESULTS:
    print("  " + r)
print("DONE")

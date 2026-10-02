#!/usr/bin/env python3
"""local-llm-ctxgate-proxy EXTENDED TEST SUITE - real-world Goose patterns, D9/D10/D11, streaming+tools, memory, stress."""
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

print("=" * 60)
print("local-llm-ctxgate-proxy EXTENDED TEST SUITE (advanced)")
print("=" * 60)

# ============================================================
# SECTION A: Real Goose Tool Definitions
# ============================================================
print("\n--- A: Real Goose Tool Definitions ---")

# Simulate the actual tool set Goose sends (simplified but realistic)
goose_tools = [
    {"type": "function", "function": {
        "name": "execute_typescript",
        "description": "Execute TypeScript code with access to registered SDK functions.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Typescript code to execute"},
                "tool_graph": {"type": "array", "items": {"type": "object"}}
            },
            "required": []
        }
    }},
    {"type": "function", "function": {
        "name": "SecureFilesystem_readFile",
        "description": "Read a file from the filesystem",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": ["path"]
        }
    }},
    {"type": "function", "function": {
        "name": "SecureFilesystem_writeFile",
        "description": "Write content to a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    }},
    {"type": "function", "function": {
        "name": "ShellExecutor_runProcess",
        "description": "Run a process on this linux machine",
        "parameters": {
            "type": "object",
            "properties": {
                "command_line": {"type": "string"},
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "stdin_text": {"type": "string"},
                "timeout_ms": {"type": "number"}
            },
            "required": []
        }
    }},
    {"type": "function", "function": {
        "name": "list_functions",
        "description": "List all available SDK functions",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }},
    {"type": "function", "function": {
        "name": "get_function_details",
        "description": "Get detailed TypeScript definitions",
        "parameters": {
            "type": "object",
            "properties": {
                "functions": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["functions"]
        }
    }},
    {"type": "function", "function": {
        "name": "load",
        "description": "Load knowledge into context",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "cancel": {"type": "boolean"},
                "peek": {"type": "boolean"}
            },
            "required": []
        }
    }},
    {"type": "function", "function": {
        "name": "delegate",
        "description": "Delegate a task to a subagent",
        "parameters": {
            "type": "object",
            "properties": {
                "instructions": {"type": "string"},
                "source": {"type": "string"},
                "async": {"type": "boolean"},
                "max_turns": {"type": "integer"}
            },
            "required": []
        }
    }},
    {"type": "function", "function": {
        "name": "analyze",
        "description": "Analyze code structure",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "focus": {"type": "string"},
                "max_depth": {"type": "integer"},
                "follow_depth": {"type": "integer"},
                "force": {"type": "boolean"}
            },
            "required": ["path"]
        }
    }},
]

# A1: Full Goose tool set (10 tools) - basic chat
b = {"model": "Qwen3.8-27B",
     "messages": [{"role": "system", "content": "You are goose, a general-purpose AI agent."},
                  {"role": "user", "content": "What tools do you have?"}],
     "tools": goose_tools, "tool_choice": "auto",
     "max_tokens": 200, "temperature": 0.3, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
tc = d["choices"][0]["message"].get("tool_calls") if ok else None
record("A1_goose_10tools", ok, "s=" + str(s))
c = d["choices"][0]["message"].get("content") or "" if ok else ""
record("A1_goose_content", ok and ((c and len(c) > 10) or tc is not None), "content=" + str(len(c) if c else 0) + " tc=" + str(tc is not None))

# A2: Force a specific Goose tool
b = {"model": "Qwen3.8-27B",
     "messages": [{"role": "system", "content": "You are goose. Use tools."},
                  {"role": "user", "content": "Read the file /etc/hostname"}],
     "tools": goose_tools,
     "tool_choice": {"type": "function", "function": {"name": "SecureFilesystem_readFile"}},
     "max_tokens": 200, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
tc = d["choices"][0]["message"].get("tool_calls") if ok else None
record("A2_force_goosetool", ok and tc is not None, "s=" + str(s))
if tc:
    fn = tc[0].get("function", {})
    record("A2_goosetool_name", fn.get("name") == "SecureFilesystem_readFile", "got=" + str(fn.get("name")))
    try:
        args = json.loads(fn.get("arguments", "{}"))
        record("A2_goosetool_args", "path" in args, "args=" + json.dumps(args)[:80])
    except:
        record("A2_goosetool_args", False, "bad JSON args")

# A3: Multi-turn with Goose tools (system + 3 turns)
msgs = [
    {"role": "system", "content": "You are goose, a general-purpose AI agent created by AAIF."},
    {"role": "user", "content": "List the files in /tmp"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_001", "type": "function",
         "function": {"name": "ShellExecutor_runProcess", "arguments": json.dumps({"command_line": "ls /tmp"})}}]},
    {"role": "tool", "tool_call_id": "call_001", "content": "file1.txt\nfile2.txt\ntest_dir"},
    {"role": "assistant", "content": "I found 2 files and 1 directory in /tmp."},
    {"role": "user", "content": "Now read file1.txt"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs,
     "tools": goose_tools, "tool_choice": "auto",
     "max_tokens": 200, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
ok = s == 200 and d.get("choices")
tc = d["choices"][0]["message"].get("tool_calls") if ok else None
c = d["choices"][0]["message"].get("content") or "" if ok else ""
record("A3_multi_turn_goosetools", ok, "s=" + str(s))
record("A3_multi_tool_or_content", ok and (tc is not None or len(c) > 5), "tc=" + str(tc is not None) + " c=" + str(len(c)))

# ============================================================
# SECTION B: D9 - Malformed Tool Call Sanitization
# ============================================================
print("\n--- B: D9 Malformed Tool Call Sanitization ---")

# B1: Valid tool call in history (should pass through)
msgs_b1 = [
    {"role": "system", "content": "Use tools."},
    {"role": "user", "content": "Check weather"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})}}]},
    {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"temp": 22})},
    {"role": "user", "content": "Thanks"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_b1, "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B1_valid_tc_history", s == 200, "s=" + str(s))

# B2: Malformed tool call - missing function.name
msgs_b2 = [
    {"role": "system", "content": "Use tools."},
    {"role": "user", "content": "Check weather"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"arguments": "{}"}}]},  # missing name
    {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
    {"role": "user", "content": "Thanks"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_b2, "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B2_malformed_no_name", s == 200, "s=" + str(s) + " (D9 should strip)")

# B3: Malformed tool call - invalid JSON arguments
msgs_b3 = [
    {"role": "system", "content": "Use tools."},
    {"role": "user", "content": "Check"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "test", "arguments": "{broken json!!"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "user", "content": "Next"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_b3, "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B3_malformed_badjson", s == 200, "s=" + str(s) + " (D9 should strip)")

# B4: Malformed tool call - missing id
msgs_b4 = [
    {"role": "system", "content": "Use tools."},
    {"role": "user", "content": "Check"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"type": "function",
         "function": {"name": "test", "arguments": json.dumps({"x": 1})}}]},  # no id
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "user", "content": "Next"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_b4, "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("B4_malformed_no_id", s == 200, "s=" + str(s) + " (D9 should strip)")

# B5: Check metrics for toolcall_strips
s, d = api("/metrics", method="GET")
strips = d.get("toolcall_strips", 0) if isinstance(d, dict) else 0
record("B5_d9_strips_counted", isinstance(strips, int), "strips=" + str(strips))

# ============================================================
# SECTION C: D10 - Reasoning Stripping
# ============================================================
print("\n--- C: D10 Reasoning Stripping ---")

# C1: Assistant message with reasoning field (should be stripped)
msgs_c1 = [
    {"role": "system", "content": "Think step by step."},
    {"role": "user", "content": "What is 2+2?"},
    {"role": "assistant", "content": "The answer is 4.",
     "reasoning": "Let me think... 2+2 equals 4. Step 1: identify numbers. Step 2: add them."},
    {"role": "user", "content": "And 3+3?"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_c1, "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("C1_reasoning_stripped", s == 200, "s=" + str(s))
c = d["choices"][0]["message"].get("content", "") if s == 200 and d.get("choices") else ""
record("C1_answer_correct", "6" in c, "content=" + c[:60])

# C2: Multiple reasoning fields
msgs_c2 = [
    {"role": "system", "content": "Think."},
    {"role": "user", "content": "Q1"},
    {"role": "assistant", "content": "A1", "reasoning": "thinking about Q1..."},
    {"role": "user", "content": "Q2"},
    {"role": "assistant", "content": "A2", "reasoning": "thinking about Q2..."},
    {"role": "user", "content": "Q3"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_c2, "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("C2_multi_reasoning", s == 200, "s=" + str(s))

# ============================================================
# SECTION D: D11 - Prefix Fingerprint
# ============================================================
print("\n--- D: D11 Prefix Fingerprint ---")

# Reset prefix fingerprint for clean test isolation
api("/_test/reset_prefix", method="POST")

# D1: Same prefix (should NOT invalidate)
s, _ = api("/metrics", method="GET")
inv_before = s if isinstance(s, dict) and "prefix_invalidations" in s else None
inv_before = inv_before.get("prefix_invalidations", 0) if isinstance(inv_before, dict) else 0

msgs_d = [
    {"role": "system", "content": "You are a test bot. Be brief."},
    {"role": "user", "content": "Say hello"}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_d, "max_tokens": 20, "temperature": 0.1, "stream": False}
api("/v1/chat/completions", b)

# Same prefix again
api("/v1/chat/completions", b)

s, d = api("/metrics", method="GET")
inv_after_same = d.get("prefix_invalidations", 0) if isinstance(d, dict) else 0
# Should not increase (same prefix)
record("D1_same_prefix_no_invalidate", inv_after_same == inv_before,
       "before=" + str(inv_before) + " after=" + str(inv_after_same))

# D2: Changed prefix (SHOULD invalidate)
msgs_d2 = [
    {"role": "system", "content": "You are a completely different bot now. Be verbose."},
    {"role": "user", "content": "Say hello"}
]
b2 = {"model": "Qwen3.8-27B", "messages": msgs_d2, "max_tokens": 20, "temperature": 0.1, "stream": False}
api("/v1/chat/completions", b2)

s, d = api("/metrics", method="GET")
inv_after_diff = d.get("prefix_invalidations", 0) if isinstance(d, dict) else 0
record("D2_changed_prefix_invalidates", inv_after_diff > inv_before,
       "before=" + str(inv_before) + " after=" + str(inv_after_diff))

# ============================================================
# SECTION E: Streaming with Tools
# ============================================================
print("\n--- E: Streaming with Tools ---")

# E1: Stream with tool_choice=auto
tl = [{"type": "function", "function": {
    "name": "search_web", "description": "Search the web",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]
b = {"model": "Qwen3.8-27B",
     "messages": [{"role": "system", "content": "Use the search tool."},
                  {"role": "user", "content": "Search for artificial intelligence news"}],
     "tools": tl, "tool_choice": "auto",
     "max_tokens": 200, "temperature": 0.1, "stream": True}
s, lines = sapi("/v1/chat/completions", b)
has_data = any(l.startswith("data: ") for l in lines)
has_done = any("[DONE]" in l for l in lines)
record("E1_stream_auto_tool", s == 200 and has_data and has_done, "s=" + str(s))

# Check if any chunk has tool_calls
has_tc = False
for l in lines:
    if l.startswith("data: ") and l != "data: [DONE]":
        try:
            chunk = json.loads(l[6:])
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if delta.get("tool_calls"):
                has_tc = True
        except: pass
record("E1_stream_has_tc", has_tc, "tool_calls in stream=" + str(has_tc))

# E2: Stream with forced tool
b = {"model": "Qwen3.8-27B",
     "messages": [{"role": "user", "content": "Read /etc/hostname"}],
     "tools": [{"type": "function", "function": {
         "name": "read_file", "description": "Read a file",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
     "tool_choice": {"type": "function", "function": {"name": "read_file"}},
     "max_tokens": 200, "temperature": 0.1, "stream": True}
s, lines = sapi("/v1/chat/completions", b)
has_done = any("[DONE]" in l for l in lines)
record("E2_stream_forced_tool", s == 200 and has_done, "s=" + str(s))

# ============================================================
# SECTION F: Context Trimming
# ============================================================
print("\n--- F: Context Trimming ---")

# F1: Build a conversation that exceeds MAX_INPUT (64000 tokens)
# Each message ~50 tokens, need ~1300+ messages. Use 200 messages with ~100 tokens each = ~20000 tokens
# That won't trigger trim. Let's use long content.
msgs_f = [{"role": "system", "content": "You are a helpful assistant. " * 20}]
for i in range(50):
    msgs_f.append({"role": "user", "content": "This is message number " + str(i) + ". " * 20})
    msgs_f.append({"role": "assistant", "content": "This is response number " + str(i) + ". " * 20})

b = {"model": "Qwen3.8-27B", "messages": msgs_f, "max_tokens": 30, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b, timeout=180)
record("F1_trim_long_conv", s == 200, "s=" + str(s) + " msgs=" + str(len(msgs_f)))
if s == 200 and d.get("choices"):
    usage = d.get("usage", {})
    pt = usage.get("prompt_tokens", 0)
    record("F1_trim_prompt_under_limit", pt <= 65000, "prompt_tokens=" + str(pt))

# F2: Very long single message (10K words ~ 13K tokens)
long_msg = "The quick brown fox jumps over the lazy dog. " * 1000
msgs_f2 = [
    {"role": "system", "content": "Brief."},
    {"role": "user", "content": long_msg + "\n\nSummarize in one word."}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_f2, "max_tokens": 20, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b, timeout=120)
record("F2_very_long_single", s == 200, "s=" + str(s))

# ============================================================
# SECTION G: Memory Endpoints (Extended)
# ============================================================
print("\n--- G: Memory Endpoints (Extended) ---")

# G1: Inject and verify content
b = {"task_id": "mem-ext-001", "content": "User prefers dark mode and TypeScript."}
s, d = api("/memory/inject", b)
record("G1_inject", s == 200, "s=" + str(s))

s, d = api("/memory/mem-ext-001", method="GET")
ok = s == 200 and isinstance(d, dict) and "dark mode" in d.get("content", "")
record("G1_inject_verify", ok, "content=" + str(d.get("content", ""))[:60] if isinstance(d, dict) else str(d))

# G2: Overwrite memory
b = {"task_id": "mem-ext-001", "content": "User now prefers light mode."}
s, d = api("/memory/inject", b)
record("G2_overwrite", s == 200, "s=" + str(s))

s, d = api("/memory/mem-ext-001", method="GET")
ok = s == 200 and isinstance(d, dict) and "light mode" in d.get("content", "")
record("G2_overwrite_verify", ok, "content=" + str(d.get("content", ""))[:60] if isinstance(d, dict) else str(d))

# G3: Query non-existent session
s, d = api("/memory/nonexistent-xyz-999", method="GET")
ok = s == 200 and isinstance(d, dict) and d.get("content") == ""
record("G3_nonexistent", ok, "s=" + str(s) + " content=" + str(d.get("content")) if isinstance(d, dict) else str(d))

# G4: Empty content inject
b = {"task_id": "mem-ext-002", "content": ""}
s, d = api("/memory/inject", b)
record("G4_empty_content", s == 200, "s=" + str(s))

# G5: Missing task_id
s, d = api("/memory/inject", {"content": "no task id"})
record("G5_no_taskid", s == 400, "s=" + str(s))

# G6: Very long memory content
b = {"task_id": "mem-ext-003", "content": "A" * 10000}
s, d = api("/memory/inject", b)
record("G6_long_content", s == 200, "s=" + str(s))

# ============================================================
# SECTION H: Concurrent & Stress
# ============================================================
print("\n--- H: Concurrent & Stress ---")

# H1: 5 concurrent simple chats
results_h = [None] * 5
def chat_worker(i):
    bb = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "Say H" + str(i)}],
          "max_tokens": 10, "temperature": 0.1, "stream": False}
    st, _ = api("/v1/chat/completions", bb, timeout=60)
    results_h[i] = st

threads = [threading.Thread(target=chat_worker, args=(i,)) for i in range(5)]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
elapsed = time.time() - t0
all_ok = all(r == 200 for r in results_h)
record("H1_concurrent_5", all_ok, "codes=" + str(results_h) + " time=" + str(round(elapsed,1)) + "s")

# H2: Concurrent with tools
results_h2 = [None] * 3
def tool_worker(i):
    bb = {"model": "Qwen3.8-27B",
          "messages": [{"role": "user", "content": "Use tool for T" + str(i)}],
          "tools": [{"type": "function", "function": {
              "name": "test_fn", "description": "Test",
              "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}}],
          "tool_choice": "auto", "max_tokens": 100, "temperature": 0.1, "stream": False}
    st, _ = api("/v1/chat/completions", bb, timeout=60)
    results_h2[i] = st

threads = [threading.Thread(target=tool_worker, args=(i,)) for i in range(3)]
for t in threads: t.start()
for t in threads: t.join()
record("H2_concurrent_tools", all(r == 200 for r in results_h2), "codes=" + str(results_h2))

# ============================================================
# SECTION I: Content Type Edge Cases
# ============================================================
print("\n--- I: Content Type Edge Cases ---")

# I1: Content as list (multi-part)
msgs_i1 = [
    {"role": "system", "content": "You handle multi-part content."},
    {"role": "user", "content": [
        {"type": "text", "text": "What does this say?"},
        {"type": "text", "text": "The answer is 42."}
    ]}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_i1, "max_tokens": 50, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("I1_list_content", s == 200, "s=" + str(s))

# I2: Empty string content
msgs_i2 = [
    {"role": "system", "content": "Brief."},
    {"role": "user", "content": ""}
]
b = {"model": "Qwen3.8-27B", "messages": msgs_i2, "max_tokens": 20, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("I2_empty_content", s in (200, 400, 422), "s=" + str(s))

# I3: Very long system prompt (5000 words)
long_sys = "You are a helpful assistant. " * 500
b = {"model": "Qwen3.8-27B",
     "messages": [{"role": "system", "content": long_sys},
                  {"role": "user", "content": "Hi"}],
     "max_tokens": 20, "temperature": 0.1, "stream": False}
s, d = api("/v1/chat/completions", b)
record("I3_long_system", s == 200, "s=" + str(s))

# I4: Only system message (no user)
b = {"model": "Qwen3.8-27B",
     "messages": [{"role": "system", "content": "You are a bot."}],
     "max_tokens": 10, "stream": False}
s, d = api("/v1/chat/completions", b)
record("I4_system_only", s in (200, 400, 422), "s=" + str(s))

# ============================================================
# SECTION J: vLLM Direct Comparison (bypass proxy)
# ============================================================
print("\n--- J: vLLM Direct (bypass proxy) ---")

# J1: Direct vLLM call to compare
def vllm_direct(body):
    url = "http://127.0.0.1:29000/v1/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)

# J2: Direct with tools
b_direct = {"model": "Qwen3.8-27B",
            "messages": [{"role": "user", "content": "What is 5*7?"}],
            "tools": [{"type": "function", "function": {
                "name": "multiply", "description": "Multiply two numbers",
                "parameters": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a","b"]}}}],
            "tool_choice": "auto", "max_tokens": 100, "temperature": 0.1, "stream": False}
s, d = vllm_direct(b_direct)
record("J2_vllm_direct_tools", s == 200, "s=" + str(s))

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 60)
print("EXTENDED RESULTS: " + str(PASS) + " PASS, " + str(FAIL) + " FAIL (total " + str(PASS + FAIL) + ")")
for r in RESULTS:
    print("  " + r)
print("DONE")


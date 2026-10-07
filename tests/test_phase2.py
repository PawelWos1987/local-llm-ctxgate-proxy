#!/usr/bin/env python3
"""Phase 2: Seed dangling tool-call repair tests."""
import sys, os, json, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
os.environ.setdefault("CTXGATE_DB_DSN", "postgresql://x@127.0.0.1:1/unused")
os.environ.setdefault("VLLM_URL", "http://127.0.0.1:19999/v1")
os.environ.setdefault("VLLM_API_KEY", "x")
os.environ["CTXGATE_REPAIR_DANGLING_TOOLCALLS"] = "1"

import app as appmod

passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")

# --- Test 1: Dangling tool_call is removed ---
msgs = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Do something"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_abc123", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
    # No tool result for call_abc123
]
result = appmod._repair_dangling_tool_calls(msgs)
check("dangling_removed", len(result) == 3 and "tool_calls" not in result[2],
      f"result[2]={json.dumps(result[2])}")
check("placeholder_added", result[2].get("content") == "[earlier tool call archived]",
      f"content={result[2].get('content')}")

# --- Test 2: Resolved tool_call is kept ---
msgs2 = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "thinking", "tool_calls": [{"id": "call_xyz", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_xyz", "content": "result here"},
]
result2 = appmod._repair_dangling_tool_calls(msgs2)
check("resolved_kept", result2[2].get("tool_calls") is not None and len(result2[2]["tool_calls"]) == 1)
check("no_placeholder", result2[2].get("content") == "thinking")

# --- Test 3: Idempotent ---
result3a = appmod._repair_dangling_tool_calls(msgs)
result3b = appmod._repair_dangling_tool_calls(result3a)
check("idempotent", json.dumps(result3a) == json.dumps(result3b))

# --- Test 4: No mutation of input ---
original = json.dumps(msgs)
_ = appmod._repair_dangling_tool_calls(msgs)
check("no_mutation", json.dumps(msgs) == original)

# --- Test 5: Mixed - one dangling, one resolved ---
msgs5 = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_dangling", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
        {"id": "call_resolved", "type": "function", "function": {"name": "read", "arguments": "{}"}},
    ]},
    {"role": "tool", "tool_call_id": "call_resolved", "content": "ok"},
]
result5 = appmod._repair_dangling_tool_calls(msgs5)
check("mixed_keeps_resolved", len(result5[2]["tool_calls"]) == 1 and result5[2]["tool_calls"][0]["id"] == "call_resolved")
check("mixed_no_placeholder", result5[2].get("content") == "")

# --- Test 6: No tool_calls at all ---
msgs6 = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
]
result6 = appmod._repair_dangling_tool_calls(msgs6)
check("no_tc_unchanged", json.dumps(result6) == json.dumps(msgs6))

# --- Test 7: Byte-identical across calls ---
h1 = hashlib.sha256(json.dumps(result3a, sort_keys=True).encode()).hexdigest()
h2 = hashlib.sha256(json.dumps(appmod._repair_dangling_tool_calls(msgs), sort_keys=True).encode()).hexdigest()
check("byte_identical", h1 == h2)

# --- Test 8: Env flag off ---
os.environ["CTXGATE_REPAIR_DANGLING_TOOLCALLS"] = "0"
# Need to reload the module to pick up the env change
import importlib
importlib.reload(appmod)
result8 = appmod._repair_dangling_tool_calls(msgs)
check("flag_off_no_repair", "tool_calls" in result8[2], f"result8[2]={json.dumps(result8[2])}")
os.environ["CTXGATE_REPAIR_DANGLING_TOOLCALLS"] = "1"
importlib.reload(appmod)

# --- Test 9: Content is list (multimodal) ---
msgs9 = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": [{"type": "text", "text": "let me check"}], "tool_calls": [
        {"id": "call_d", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
    ]},
]
result9 = appmod._repair_dangling_tool_calls(msgs9)
check("multimodal_kept", "tool_calls" not in result9[2])
check("multimodal_content_intact", result9[2].get("content") == [{"type": "text", "text": "let me check"}])

print(f"\n=== Phase 2: {passed} passed, {failed} failed ===")
sys.exit(0 if failed == 0 else 1)

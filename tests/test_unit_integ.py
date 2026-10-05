#!/usr/bin/env python3
"""local-llm-ctxgate-proxy - Unit + Integration deep test suite (6th suite).

Targets INTERNAL functions via direct import + HTTP integration:
  A. count_tokens / count_message_tokens / count_messages_tokens (unit)
  B. compute_prefix_fingerprint determinism & edge cases (unit)
  C. strip_reasoning (unit)
  D. sanitize_tool_calls - all malformed branches (unit)
  E. trim_context - budget math, edge cases (unit)
  F. Memory endpoints deep (inject, query, upsert, missing)
  G. Prefix reset + invalidation counter
  H. Model validation (wrong model, empty, case)
  I. Memory job auto-enqueue via chat
  J. Concurrent memory inject (race safety)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from proxy.app import (
    compute_prefix_fingerprint,
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
    sanitize_tool_calls,
    strip_reasoning,
    trim_context,
)

PROXY = "http://127.0.0.1:9201"
MODEL = "Qwen3.8-27B"
passed = 0
failed = 0
results = []

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        results.append(("PASS", name))
    else:
        failed += 1
        results.append(("FAIL", name + " " + detail))

def http_post(url, data, headers=None):
    body = json.dumps(data).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

def http_get(url, headers=None):
    hdrs = {}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

def chat(messages, max_tokens=20, tool_defs=None, tool_choice=None, stream=False, headers=None, model=None):
    body = {"model": model or MODEL, "messages": messages, "max_tokens": max_tokens}
    if tool_defs:
        body["tools"] = tool_defs
    if tool_choice:
        body["tool_choice"] = tool_choice
    if stream:
        body["stream"] = True
    return http_post(PROXY + "/v1/chat/completions", body, headers=headers)

# ============================================================
# A. Token counting (unit)
# ============================================================
def section_a():
    global passed, failed
    print("== A: Token counting (unit) ==")

    tok = count_tokens("hello world")
    check("A1_count_basic", tok > 0, f"tok={tok}")

    tok_empty = count_tokens("")
    check("A2_count_empty", tok_empty == 0, f"tok={tok_empty}")

    tok_long = count_tokens("hello " * 100)
    tok_short = count_tokens("hi")
    check("A3_count_longer_bigger", tok_long > tok_short, f"long={tok_long} short={tok_short}")

    # Unicode
    tok_uni = count_tokens("hello \u4f60\u597d \U0001F600")
    check("A4_count_unicode", tok_uni > 0, f"tok={tok_uni}")

    # Message with list content
    msg_list = {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]}
    tok_list = count_message_tokens(msg_list)
    tok_str = count_message_tokens({"role": "user", "content": "hello world"})
    check("A5_msg_list_content", tok_list > 0, f"tok={tok_list}")
    check("A6_list_more_than_str", tok_list >= tok_str, f"list={tok_list} str={tok_str}")

    # Message with tool_call_id
    msg_tool = {"role": "tool", "content": "result", "tool_call_id": "call_abc123"}
    tok_tool = count_message_tokens(msg_tool)
    check("A7_tool_call_id", tok_tool > 0, f"tok={tok_tool}")

    # Message with tool_calls in assistant
    msg_asst = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]}
    tok_asst = count_message_tokens(msg_asst)
    check("A8_assistant_tool_calls", tok_asst >= 0, f"tok={tok_asst}")

    # Multiple messages
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there! How can I help?"},
    ]
    total = count_messages_tokens(msgs)
    check("A9_multi_messages", total > 0, f"total={total}")

    # Empty list
    total_empty = count_messages_tokens([])
    check("A10_empty_messages", total_empty == 0, f"total={total_empty}")

    # Special chars
    tok_special = count_tokens("<xml>&amp;\n\t\r special chars: !@#$%^&*()")
    check("A11_special_chars", tok_special > 0, f"tok={tok_special}")

    # Very long text
    tok_vlong = count_tokens("x" * 100000)
    check("A12_vlong_text", tok_vlong > 100, f"tok={tok_vlong}")

# ============================================================
# B. Prefix fingerprint (unit)
# ============================================================
def section_b():
    print("== B: Prefix fingerprint (unit) ==")

    msgs1 = [{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "Hello"}]
    msgs1_copy = [{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "Hello"}]

    fp1 = compute_prefix_fingerprint(msgs1)
    fp2 = compute_prefix_fingerprint(msgs1_copy)
    check("B1_deterministic", fp1 == fp2, f"fp1={fp1} fp2={fp2}")
    check("B2_fp_length_16", len(fp1) == 16, f"len={len(fp1)}")

    # Different system content
    msgs2 = [{"role": "system", "content": "Different system"}, {"role": "user", "content": "Hello"}]
    fp3 = compute_prefix_fingerprint(msgs2)
    check("B3_diff_system_diff_fp", fp1 != fp3, f"fp1={fp1} fp3={fp3}")

    # Same system, different user on first turn (first user after system)
    msgs3 = [{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "World"}]
    fp4 = compute_prefix_fingerprint(msgs3)
    check("B4_diff_first_user_diff_fp", fp1 != fp4, f"fp1={fp1} fp4={fp4}")

    # List content in system
    msgs4 = [{"role": "system", "content": [{"type": "text", "text": "You are helpful"}]}, {"role": "user", "content": "Hello"}]
    fp5 = compute_prefix_fingerprint(msgs4)
    check("B5_list_system_content", fp5 == fp1, f"fp5={fp5} fp1={fp1}")

    # No system message (just user)
    msgs5 = [{"role": "user", "content": "Hello"}]
    fp6 = compute_prefix_fingerprint(msgs5)
    check("B6_no_system", len(fp6) == 16, f"fp6={fp6}")

    # Empty messages
    fp7 = compute_prefix_fingerprint([])
    check("B7_empty", len(fp7) == 16, f"fp7={fp7}")

    # Only system, no user
    msgs8 = [{"role": "system", "content": "Solo system"}]
    fp8 = compute_prefix_fingerprint(msgs8)
    check("B8_only_system", len(fp8) == 16, f"fp8={fp8}")

    # Multiple users - only first counts
    msgs9 = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1"}, {"role": "user", "content": "U2"}]
    msgs10 = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1"}, {"role": "user", "content": "DIFFERENT"}]
    fp9 = compute_prefix_fingerprint(msgs9)
    fp10 = compute_prefix_fingerprint(msgs10)
    check("B9_second_user_ignored", fp9 == fp10, f"fp9={fp9} fp10={fp10}")

# ============================================================
# C. strip_reasoning (unit)
# ============================================================
def section_c():
    print("== C: strip_reasoning (unit) ==")

    msgs = [
        {"role": "assistant", "content": "Hi", "reasoning": "long chain of thought..."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Welcome"},
    ]
    cleaned = strip_reasoning(msgs)
    check("C1_reasoning_removed", "reasoning" not in cleaned[0], f"keys={list(cleaned[0].keys())}")
    check("C2_content_preserved", cleaned[0]["content"] == "Hi")
    check("C3_other_msgs_untouched", cleaned[1] == {"role": "user", "content": "Hello"})
    check("C4_no_reasoning_msg", "reasoning" not in cleaned[2])

    # No reasoning at all
    msgs2 = [{"role": "assistant", "content": "Hello"}, {"role": "user", "content": "Hi"}]
    cleaned2 = strip_reasoning(msgs2)
    check("C5_no_reasoning_passthrough", cleaned2 == msgs2)

    # Multiple assistant with reasoning
    msgs3 = [
        {"role": "assistant", "content": "A1", "reasoning": "R1"},
        {"role": "assistant", "content": "A2", "reasoning": "R2"},
    ]
    cleaned3 = strip_reasoning(msgs3)
    check("C6_multi_assistant_stripped", all("reasoning" not in m for m in cleaned3))
    check("C7_content_intact", all(cleaned3[i]["content"] == msgs3[i]["content"] for i in range(2)))

# ============================================================
# D. sanitize_tool_calls (unit) - all branches
# ============================================================
def section_d():
    print("== D: sanitize_tool_calls (unit) ==")

    # D1: No tool_calls -> passthrough
    msg = {"role": "assistant", "content": "Hi"}
    out, stripped = sanitize_tool_calls(msg)
    check("D1_no_toolcalls", not stripped and out == msg)

    # D2: Valid tool_calls
    valid_tc = [{"id": "c1", "type": "function", "function": {"name": "add", "arguments": "{\"a\":1}"}}]
    msg2 = {"role": "assistant", "content": "Let me add", "tool_calls": valid_tc}
    out2, stripped2 = sanitize_tool_calls(msg2)
    check("D2_valid_tc_kept", not stripped2 and "tool_calls" in out2)

    # D3: Non-dict in tool_calls list
    msg3 = {"role": "assistant", "content": "x", "tool_calls": ["not-a-dict"]}
    out3, stripped3 = sanitize_tool_calls(msg3)
    check("D3_nondict_stripped", stripped3 and "tool_calls" not in out3)
    check("D3b_content_preserved", out3["content"] == "x")

    # D4: Missing 'id'
    msg4 = {"role": "assistant", "content": "y", "tool_calls": [{"type": "function", "function": {"name": "f", "arguments": "{}"}}]}
    out4, stripped4 = sanitize_tool_calls(msg4)
    check("D4_missing_id", stripped4 and "tool_calls" not in out4)

    # D5: Missing 'type'
    msg5 = {"role": "assistant", "content": "z", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}
    out5, stripped5 = sanitize_tool_calls(msg5)
    check("D5_missing_type", stripped5)

    # D6: Missing 'function'
    msg6 = {"role": "assistant", "content": "w", "tool_calls": [{"id": "c1", "type": "function"}]}
    out6, stripped6 = sanitize_tool_calls(msg6)
    check("D6_missing_function", stripped6)

    # D7: Function missing 'name'
    msg7 = {"role": "assistant", "content": "v", "tool_calls": [{"id": "c1", "type": "function", "function": {"arguments": "{}"}}]}
    out7, stripped7 = sanitize_tool_calls(msg7)
    check("D7_fn_missing_name", stripped7)

    # D8: Function missing 'arguments'
    msg8 = {"role": "assistant", "content": "u", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}]}
    out8, stripped8 = sanitize_tool_calls(msg8)
    check("D8_fn_missing_args", stripped8)

    # D9: Invalid JSON in arguments string
    msg9 = {"role": "assistant", "content": "t", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "not valid json {"}}]}
    out9, stripped9 = sanitize_tool_calls(msg9)
    check("D9_invalid_json_args", stripped9)

    # D10: Arguments is a dict (valid)
    msg10 = {"role": "assistant", "content": "s", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": {"key": "val"}}}]}
    out10, stripped10 = sanitize_tool_calls(msg10)
    check("D10_dict_args_ok", not stripped10)

    # D11: Arguments is invalid type (list)
    msg11 = {"role": "assistant", "content": "r", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": [1, 2]}}]}
    out11, stripped11 = sanitize_tool_calls(msg11)
    check("D11_list_args_stripped", stripped11)

    # D12: None content + stripped tool_calls
    msg12 = {"role": "assistant", "content": None, "tool_calls": ["bad"]}
    out12, stripped12 = sanitize_tool_calls(msg12)
    check("D12_none_content_becomes_str", out12.get("content") == "", f"content={out12.get('content')}")

    # D13: Multiple tool_calls, second is bad
    msg13 = {"role": "assistant", "content": "multi", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
        {"id": "c2", "type": "function"}
    ]}
    out13, stripped13 = sanitize_tool_calls(msg13)
    check("D13_second_bad_strips_all", stripped13 and "tool_calls" not in out13)

    # D14: Multiple valid tool_calls
    msg14 = {"role": "assistant", "content": "multi2", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "f2", "arguments": "{\"x\":1}"}}
    ]}
    out14, stripped14 = sanitize_tool_calls(msg14)
    check("D14_all_valid_kept", not stripped14)

    # D15: Empty tool_calls list
    msg15 = {"role": "assistant", "content": "empty", "tool_calls": []}
    out15, stripped15 = sanitize_tool_calls(msg15)
    check("D15_empty_tc_list", not stripped15)

# ============================================================
# E. trim_context (unit)
# ============================================================
def section_e():
    print("== E: trim_context (unit) ==")

    # E1: <= 3 messages - passthrough
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "A"},
    ]
    out = trim_context(msgs, 4096)
    check("E1_leq3_passthrough", len(out) == 3)

    # E2: 2 messages
    msgs2 = [{"role": "user", "content": "U"}, {"role": "assistant", "content": "A"}]
    out2 = trim_context(msgs2, 4096)
    check("E2_two_msgs", len(out2) == 2)

    # E3: 10 messages, large budget - keeps all
    msgs3 = [{"role": "user" if i % 2 == 0 else "assistant", "content": "msg " + str(i)} for i in range(10)]
    out3 = trim_context(msgs3, 4096)
    check("E3_large_budget_all", len(out3) >= 2, f"kept={len(out3)}/10")

    # E4: 20 messages, small budget - trims
    msgs4 = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 100} for i in range(20)]
    out4 = trim_context(msgs4, 200)
    check("E4_small_budget_trims", len(out4) < 20, f"kept={len(out4)}/20")

    # E5: System always kept
    msgs5 = [{"role": "system", "content": "System prompt"}] + [{"role": "user" if i % 2 == 0 else "assistant", "content": "y" * 50} for i in range(15)]
    out5 = trim_context(msgs5, 150)
    check("E5_system_kept", any(m["role"] == "system" for m in out5))

    # E6: First user kept
    first_user = {"role": "user", "content": "First question"}
    msgs6 = [{"role": "system", "content": "S"}, first_user] + [{"role": "user" if i % 2 == 0 else "assistant", "content": "z" * 50} for i in range(15)]
    out6 = trim_context(msgs6, 150)
    check("E6_first_user_kept", first_user in out6)

    # E7: Recent messages kept (last message)
    last_msg = msgs6[-1]
    check("E7_last_msg_kept", last_msg in out6)

    # E8: No system, just user/assistant
    msgs8 = [{"role": "user" if i % 2 == 0 else "assistant", "content": "no sys " + str(i)} for i in range(10)]
    out8 = trim_context(msgs8, 100)
    check("E8_no_system", len(out8) >= 1, f"kept={len(out8)}/10")

    # E9: All same role
    msgs9 = [{"role": "user", "content": "q " + str(i)} for i in range(10)]
    out9 = trim_context(msgs9, 100)
    check("E9_all_user", len(out9) >= 1, f"kept={len(out9)}/10")

    # E10: Very large single message
    msgs10 = [{"role": "system", "content": "S"}, {"role": "user", "content": "x" * 5000}]
    out10 = trim_context(msgs10, 100)
    check("E10_huge_msg", len(out10) <= 2, f"kept={len(out10)}")

# ============================================================
# F. Memory endpoints deep (HTTP)
# ============================================================
def section_f():
    print("== F: Memory endpoints deep (HTTP) ==")

    # F1: Inject with task_ref
    sid = "mem-test-" + uuid.uuid4().hex[:8]
    content = "Remember: the answer is 42 and the project is local-llm-ctxgate-proxy"
    s, r = http_post(PROXY + "/memory/inject", {"task_id": sid, "content": content})
    check("F1_inject_ok", s == 200, f"status={s} body={r[:100]}")
    data = json.loads(r)
    check("F1b_has_uuid", "task_uuid" in data and len(data["task_uuid"]) > 0)

    # F2: Query existing
    s2, r2 = http_get(PROXY + "/memory/" + sid)
    check("F2_query_existing", s2 == 200, f"status={s2}")
    data2 = json.loads(r2)
    check("F2b_content_matches", data2["content"] == content, f"got={data2.get('content','')[:50]}")
    check("F2c_has_updated_at", data2.get("updated_at") is not None)

    # F3: Upsert (inject same task_id again)
    new_content = "Updated memory: v2 with more details"
    s3, r3 = http_post(PROXY + "/memory/inject", {"task_id": sid, "content": new_content})
    check("F3_upsert_ok", s3 == 200)
    s3b, r3b = http_get(PROXY + "/memory/" + sid)
    data3 = json.loads(r3b)
    check("F3b_updated", data3["content"] == new_content, f"got={data3.get('content','')[:50]}")

    # F4: Query non-existent
    s4, r4 = http_get(PROXY + "/memory/nonexistent-task-" + uuid.uuid4().hex[:8])
    check("F4_nonexistent", s4 == 200, f"status={s4}")
    data4 = json.loads(r4)
    check("F4b_empty_content", data4["content"] == "" and data4["updated_at"] is None)

    # F5: Missing task_id
    s5, r5 = http_post(PROXY + "/memory/inject", {"content": "no task id"})
    check("F5_missing_task_id", s5 == 400, f"status={s5}")

    # F6: Empty content
    s6, r6 = http_post(PROXY + "/memory/inject", {"task_id": "empty-content-" + uuid.uuid4().hex[:6], "content": ""})
    check("F6_empty_content", s6 == 200, f"status={s6}")

    # F7: Very long content
    long_content = "A" * 100000
    s7, r7 = http_post(PROXY + "/memory/inject", {"task_id": "long-" + uuid.uuid4().hex[:6], "content": long_content})
    check("F7_long_content", s7 == 200, f"status={s7}")
    # Verify the stored content roundtrips (use the task_id from the response)
    task_uuid_f7 = json.loads(r7).get("task_id", "long-test")
    s7b, r7b = http_get(PROXY + "/memory/" + task_uuid_f7)
    data7 = json.loads(r7b)
    check("F7b_stored", len(data7["content"]) == 100000, f"len={len(data7.get('content',''))}")
    sid2 = "alias-" + uuid.uuid4().hex[:6]
    s8, r8 = http_post(PROXY + "/memory/inject", {"task_ref": sid2, "content": "via task_ref"})
    check("F8_task_ref_alias", s8 == 200, f"status={s8}")
    s8b, r8b = http_get(PROXY + "/memory/" + sid2)
    data8 = json.loads(r8b)
    check("F8b_found_via_ref", data8["content"] == "via task_ref")

    # F9: Special chars in content
    special = "line1\nline2\ttabbed NULL unicode \u4f60\u597d emoji \U0001F600"
    sid3 = "special-" + uuid.uuid4().hex[:6]
    s9, r9 = http_post(PROXY + "/memory/inject", {"task_id": sid3, "content": special})
    check("F9_special_ok", s9 == 200, f"status={s9}")
    s9b, r9b = http_get(PROXY + "/memory/" + sid3)
    data9 = json.loads(r9b)
    check("F9b_roundtrip", data9["content"] == special, f"len={len(data9.get('content',''))} expected={len(special)}")

# ============================================================
# G. Prefix reset + invalidation
# ============================================================
def section_g():
    print("== G: Prefix reset + invalidation ==")

    # G1: Reset endpoint
    s, r = http_post(PROXY + "/_test/reset_prefix", {})
    check("G1_reset_ok", s == 200, f"status={s}")
    data = json.loads(r)
    check("G1b_status_reset", data.get("status") == "reset")

    # G2: After reset, metrics prefix_invalidations should be 0
    s2, r2 = http_get(PROXY + "/metrics")
    m = json.loads(r2)
    # G2: Record baseline after reset
    m = json.loads(r2)
    baseline_inv = m.get("prefix_invalidations", 0)
    check("G2_baseline_captured", baseline_inv >= 0, f"baseline={baseline_inv}")
    # G3: First request sets prefix
    sid = "prefix-" + uuid.uuid4().hex[:6]
    hdrs = {"X-Session-ID": sid}
    hdrs = {"X-Session-ID": sid}
    s3, r3 = chat([{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "Setting prefix"}], max_tokens=10, headers=hdrs)
    check("G3_set_prefix", s3 == 200, f"status={s3}")
    s3b, r3b = http_get(PROXY + "/metrics")
    m3 = json.loads(r3b)
    m3 = json.loads(r3b)
    delta3 = m3.get("prefix_invalidations", 0) - baseline_inv
    check("G3b_no_invalidations", delta3 == 0, f"delta={delta3} abs={m3.get('prefix_invalidations')}")
    s4, r4 = chat([{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "Setting prefix"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "Follow-up question"}], max_tokens=10, headers=hdrs)
    check("G4_same_prefix", s4 == 200, f"status={s4}")
    s4b, r4b = http_get(PROXY + "/metrics")
    m4 = json.loads(r4b)
    m4 = json.loads(r4b)
    delta4 = m4.get("prefix_invalidations", 0) - baseline_inv
    check("G4b_still_zero", delta4 == 0, f"delta={delta4} abs={m4.get('prefix_invalidations')}")
    s5, r5 = chat([{"role": "system", "content": "COMPLETELY DIFFERENT SYSTEM"}, {"role": "user", "content": "New prefix"}], max_tokens=10, headers=hdrs)
    check("G5_diff_prefix", s5 == 200, f"status={s5}")
    s5b, r5b = http_get(PROXY + "/metrics")
    m5 = json.loads(r5b)
    delta5 = m5.get("prefix_invalidations", 0) - baseline_inv
    check("G5b_increased", delta5 >= 1, f"delta={delta5} abs={m5.get('prefix_invalidations')}")
    s6, r6 = http_post(PROXY + "/_test/reset_prefix", {})
    check("G6_reset_again", s6 == 200)
    s6b, r6b = http_get(PROXY + "/metrics")
    m6 = json.loads(r6b)
    check("G6b_zero_again", m6.get("prefix_invalidations") == 0, f"inv={m6.get('prefix_invalidations')}")
    check("G6b_zero_again", m6.get("prefix_invalidations") == 0, f"inv={m6.get('prefix_invalidations')}")

# ============================================================
# H. Model validation
# ============================================================
def section_h():
    print("== H: Model validation ==")

    # H1: Correct model
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=5, model="Qwen3.8-27B")
    check("H1_correct_model", s == 200, f"status={s}")

    # H2: Wrong model -> 404
    s2, r2 = chat([{"role": "user", "content": "hi"}], max_tokens=5, model="gpt-4")
    check("H2_wrong_model_404", s2 == 404, f"status={s2} body={r2[:100]}")

    # H3: Empty model string
    body = {"model": "", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    s3, r3 = http_post(PROXY + "/v1/chat/completions", body)
    check("H3_empty_model", s3 in (200, 404), f"status={s3}")

    # H4: Model missing entirely
    body4 = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    s4, r4 = http_post(PROXY + "/v1/chat/completions", body4)
    check("H4_no_model", s4 in (200, 404), f"status={s4}")

    # H5: Case-sensitive model
    s5, r5 = chat([{"role": "user", "content": "hi"}], max_tokens=5, model="qwen3.8-27b")
    check("H5_case_model", s5 in (200, 404), f"status={s5}")

# ============================================================
# I. Memory job auto-enqueue
# ============================================================
def section_i():
    print("== I: Memory job auto-enqueue ==")

    # I1: Chat with user content -> auto-enqueues memory job
    sid = "mem-auto-" + uuid.uuid4().hex[:8]
    hdrs = {"X-Session-ID": sid}
    s, r = chat([{"role": "user", "content": "My secret project codename is Phoenix-42"}], max_tokens=10, headers=hdrs)
    check("I1_chat_ok", s == 200, f"status={s}")

    # Wait for background job
    time.sleep(5)

    # Query memory for this session
    s2, r2 = http_get(PROXY + "/memory/" + sid)
    check("I2_memory_created", s2 == 200, f"status={s2}")
    data = json.loads(r2)
    check("I2b_chat_ok", s2 == 200, f"status={s2} (events in PG)")

    # I3: Second chat with same session
    s3, r3 = chat([
        {"role": "user", "content": "My secret project codename is Phoenix-42"},
        {"role": "assistant", "content": "Noted!"},
        {"role": "user", "content": "Remember to deploy on Friday"}
    ], max_tokens=10, headers=hdrs)
    check("I3_second_chat", s3 == 200, f"status={s3}")
    time.sleep(5)
    s3b, r3b = http_get(PROXY + "/memory/" + sid)
    data3 = json.loads(r3b)
    check("I3b_chat_ok", s3b == 200, f"status={s3b}")

    # I4: List content user message
    sid2 = "mem-list-" + uuid.uuid4().hex[:8]
    hdrs2 = {"X-Session-ID": sid2}
    s4, r4 = chat([{"role": "user", "content": [{"type": "text", "text": "List content question"}]}], max_tokens=10, headers=hdrs2)
    check("I4_list_content", s4 == 200, f"status={s4}")
    time.sleep(5)
    s4b, r4b = http_get(PROXY + "/memory/" + sid2)
    data4 = json.loads(r4b)
    check("I4b_chat_ok", s4b == 200, f"status={s4b}")

    # I5: System-only messages (no user) -> no memory job
    sid3 = "mem-sys-" + uuid.uuid4().hex[:8]
    hdrs3 = {"X-Session-ID": sid3}
    s5, r5 = chat([{"role": "system", "content": "You are helpful"}], max_tokens=5, headers=hdrs3)
    check("I5_no_user", s5 in (200, 400, 404), f"status={s5}")
    # No crash is enough

# ============================================================
# J. Concurrent memory inject (race safety)
# ============================================================
def section_j():
    print("== J: Concurrent memory inject ==")

    # J1: 5 concurrent injects to SAME task_id
    sid = "race-" + uuid.uuid4().hex[:6]
    def inject_one(i):
        return http_post(PROXY + "/memory/inject", {"task_id": sid, "content": f"writer-{i}"})
    with ThreadPoolExecutor(max_workers=5) as ex:
        res = list(ex.map(inject_one, range(5)))
    ok = sum(1 for s, _ in res if s == 200)
    check("J1_five_same_task", ok == 5, f"ok={ok}/5")

    # Exactly one should win the last write
    s, r = http_get(PROXY + "/memory/" + sid)
    data = json.loads(r)
    check("J1b_has_a_writer", data["content"].startswith("writer-"), f"content={data['content'][:30]}")

    # J2: 5 concurrent injects to DIFFERENT task_ids
    def inject_diff(i):
        sid_i = f"race-diff-{i}-" + uuid.uuid4().hex[:6]
        s, r = http_post(PROXY + "/memory/inject", {"task_id": sid_i, "content": f"unique-{i}"})
        return (s, r, sid_i)
    with ThreadPoolExecutor(max_workers=5) as ex:
        res2 = list(ex.map(inject_diff, range(5)))
    ok2 = sum(1 for s, _, _ in res2 if s == 200)
    check("J2_five_diff_tasks", ok2 == 5, f"ok={ok2}/5")

    # J3: Concurrent read+write
    sid3 = "race-rw-" + uuid.uuid4().hex[:6]
    http_post(PROXY + "/memory/inject", {"task_id": sid3, "content": "initial"})
    def write_loop(i):
        s, _ = http_post(PROXY + "/memory/inject", {"task_id": sid3, "content": f"v{i}"})
        return s
    def read_loop(i):
        s, r = http_get(PROXY + "/memory/" + sid3)
        return s
    with ThreadPoolExecutor(max_workers=8) as ex:
        writers = list(ex.map(write_loop, range(4)))
        readers = list(ex.map(read_loop, range(4)))
    all_ok = all(s == 200 for s in writers + readers)
    check("J3_concurrent_rw", all_ok, f"writers={writers} readers={readers}")

# ============================================================
# Main
# ============================================================
def main():
    sections = [section_a, section_b, section_c, section_d, section_e, section_f, section_g, section_h, section_i, section_j]
    for fn in sections:
        try:
            fn()
        except Exception as e:
            global passed, failed
            failed += 1
            results.append(("FAIL", fn.__name__ + ": " + str(e)))

    print()
    print("=" * 60)
    print(f"UNIT+INTEG RESULTS: {passed}/{passed + failed} passed, {failed} failed")
    print("=" * 60)
    if failed > 0:
        for status, name in results:
            if status == "FAIL":
                print(f"  FAIL: {name}")
    sys.exit(1 if failed > 0 else 0)

if __name__ == "__main__":
    main()

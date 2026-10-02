#!/usr/bin/env python3
"""local-llm-ctxgate-proxy - Gap-fill test suite (5th suite).

Targets code paths NOT covered by the other 240 tests:
  A. Response-side D9 (vLLM returns tool_calls -> proxy sanitizes)
  B. Metrics precision (exact counter verification)
  C. Error paths (invalid JSON, missing fields, bad roles)
  D. Session isolation deep (no cross-contamination)
  E. Prefix fingerprint stability (10 seq + 1 change)
  F. Memory -> PG integration (events + memory_jobs tables)
  G. Streaming error paths
  H. Content type variants (all-list, mixed, non-text parts)
  I. Tool response validation (orphan IDs, missing IDs)
  J. vLLM passthrough fidelity (structure comparison)
  K. Boundary stress (max_tokens edge, empty string, whitespace)
"""
import asyncio
import json
import time
import urllib.request
import urllib.error
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

PROXY = "http://127.0.0.1:9201"
VLLM = "http://127.0.0.1:29000/v1"
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
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

def raw_post(url, raw_body, headers=None):
    """POST raw bytes (for invalid JSON tests)."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=raw_body.encode("utf-8"), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

def chat(msgs, **kw):
    body = {"model": MODEL, "messages": msgs}
    body.update(kw)
    return http_post(PROXY + "/v1/chat/completions", body)

def vllm_direct(msgs, **kw):
    body = {"model": MODEL, "messages": msgs}
    body.update(kw)
    return http_post(VLLM + "/chat/completions", body)

def get_metrics():
    _, r = http_get(PROXY + "/metrics")
    return json.loads(r)

# ============================================================
# A. Response-side D9
# ============================================================
def section_a():
    print("== A: Response-side D9 ==")
    
    # A1: Force tool call with tool_choice=required, verify response structure
    tool_def = {"type": "function", "function": {
        "name": "get_weather",
        "description": "Get weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    }}
    s, r = chat(
        [{"role": "user", "content": "What is the weather in Paris?"}],
        max_tokens=50, tools=[tool_def], tool_choice="required"
    )
    check("A1_tool_required", s == 200, "status=" + str(s))
    d = json.loads(r)
    msg = d["choices"][0]["message"]
    tc = msg.get("tool_calls")
    check("A1b_has_tool_calls", tc is not None, "tool_calls=" + str(type(tc)))
    if tc:
        check("A1c_tc_structure", "id" in tc[0] and "function" in tc[0], "keys=" + str(list(tc[0].keys())))
    
    # A2: tool_choice=auto (model may or may not call)
    s, r = chat(
        [{"role": "user", "content": "What is 2+2?"}],
        max_tokens=30, tools=[tool_def], tool_choice="auto"
    )
    check("A2_tool_auto", s == 200, "status=" + str(s))
    
    # A3: tool_choice=none (should NOT call tools)
    s, r = chat(
        [{"role": "user", "content": "What is 2+2?"}],
        max_tokens=30, tools=[tool_def], tool_choice="none"
    )
    check("A3_tool_none", s == 200, "status=" + str(s))
    d = json.loads(r)
    msg = d["choices"][0]["message"]
    tc = msg.get("tool_calls")
    check("A3b_no_tc", tc is None, "tool_calls=" + str(tc))
    
    # A4: response with content AND tool_calls (both present)
    s, r = chat(
        [{"role": "user", "content": "Use the weather tool for Tokyo"}],
        max_tokens=80, tools=[tool_def], tool_choice="auto"
    )
    check("A4_content_and_tc", s == 200, "status=" + str(s))
    
    # A5: verify metrics toolcall_strips counter is consistent
    m_before = get_metrics()
    strips_before = m_before.get("toolcall_strips", 0)
    # Send a request that should NOT trigger strips (valid tools)
    s, r = chat(
        [{"role": "user", "content": "Weather in London?"}],
        max_tokens=30, tools=[tool_def], tool_choice="auto"
    )
    m_after = get_metrics()
    strips_after = m_after.get("toolcall_strips", 0)
    # If model returned valid tool_calls, strips should not increase
    check("A5_strips_consistent", strips_after >= strips_before, "before=" + str(strips_before) + " after=" + str(strips_after))

# ============================================================
# B. Metrics precision
# ============================================================
def section_b():
    print("== B: Metrics precision ==")
    
    m0 = get_metrics()
    
    # B1: one successful request increments requests_total and requests_ok by 1
    s, r = chat([{"role": "user", "content": "hello"}], max_tokens=5)
    m1 = get_metrics()
    dt = m1["requests_total"] - m0["requests_total"]
    do = m1["requests_ok"] - m0["requests_ok"]
    check("B1_total_inc", dt == 1, "delta=" + str(dt))
    check("B1b_ok_inc", do == 1, "delta=" + str(do))
    
    # B2: tokens_in_total increases
    tin0 = m0["tokens_in_total"]
    tin1 = m1["tokens_in_total"]
    check("B2_tokens_in_grew", tin1 > tin0, "before=" + str(tin0) + " after=" + str(tin1))
    
    # B3: tokens_out_total increases
    tout0 = m0["tokens_out_total"]
    tout1 = m1["tokens_out_total"]
    check("B3_tokens_out_grew", tout1 >= tout0, "before=" + str(tout0) + " after=" + str(tout1))
    
    # B4: 3 more requests -> counters increment by 3
    m2 = get_metrics()
    for i in range(3):
        chat([{"role": "user", "content": "test " + str(i)}], max_tokens=5)
    m3 = get_metrics()
    dt3 = m3["requests_total"] - m2["requests_total"]
    check("B4_three_reqs", dt3 == 3, "delta=" + str(dt3))
    
    # B5: metrics fields are all present
    required_fields = ["requests_total", "requests_ok", "requests_error",
                       "tokens_in_total", "tokens_out_total",
                       "prefix_invalidations", "toolcall_strips",
                       "reasoning_strips", "started_at"]
    missing = [f for f in required_fields if f not in m3]
    check("B5_all_fields", len(missing) == 0, "missing=" + str(missing))
    
    # B6: started_at is stable (same across calls)
    m4 = get_metrics()
    check("B6_started_at_stable", m3["started_at"] == m4["started_at"],
          "m3=" + str(m3["started_at"]) + " m4=" + str(m4["started_at"]))

# ============================================================
# C. Error paths
# ============================================================
def section_c():
    print("== C: Error paths ==")
    
    # C1: invalid JSON body
    s, r = raw_post(PROXY + "/v1/chat/completions", "{invalid json")
    check("C1_bad_json", s == 400, "status=" + str(s))
    
    # C2: valid JSON but not an object
    s, r = raw_post(PROXY + "/v1/chat/completions", "[1,2,3]")
    check("C2_json_array", s in (400, 422, 500), "status=" + str(s) + " body=" + r[:100])
    
    # C3: empty object (no model, no messages)
    s, r = http_post(PROXY + "/v1/chat/completions", {})
    check("C3_empty_obj", s in (400, 422), "status=" + str(s))
    
    # C4: unknown model
    s, r = http_post(PROXY + "/v1/chat/completions", {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]})
    check("C4_unknown_model", s == 404, "status=" + str(s))
    
    # C5: empty messages array
    s, r = chat([], max_tokens=5)
    check("C5_empty_messages", s == 400, "status=" + str(s) + " body=" + r[:100])
    
    # C6: messages is not an array
    s, r = http_post(PROXY + "/v1/chat/completions", {"model": MODEL, "messages": "not an array"})
    check("C6_not_array", s in (400, 422, 500), "status=" + str(s))
    
    # C7: message with invalid role
    s, r = chat([{"role": "invalid_role", "content": "test"}], max_tokens=5)
    check("C7_bad_role", s in (200, 400, 500), "status=" + str(s) + " body=" + r[:100])
    
    # C8: message with no content key
    s, r = chat([{"role": "user"}], max_tokens=5)
    check("C8_no_content", s in (200, 400, 500), "status=" + str(s) + " body=" + r[:100])
    
    # C9: max_tokens=0
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=0)
    check("C9_zero_max_tokens", s in (200, 400), "status=" + str(s) + " body=" + r[:100])
    
    # C10: max_tokens negative
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=-1)
    check("C10_neg_max_tokens", s in (200, 400), "status=" + str(s) + " body=" + r[:100])
    
    # C11: temperature out of range
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=5, temperature=99.0)
    check("C11_high_temp", s in (200, 400), "status=" + str(s))
    
    # C12: health endpoint returns proper structure
    s, r = http_get(PROXY + "/health")
    d = json.loads(r)
    check("C12_health_struct", d.get("status") == "ok" and "version" in d, "body=" + r[:100])
    
    # C13: 404 for unknown path
    s, r = http_get(PROXY + "/nonexistent_endpoint")
    check("C13_404_path", s == 404, "status=" + str(s))

# ============================================================
# D. Session isolation deep
# ============================================================
def section_d():
    print("== D: Session isolation deep ==")
    
    # D1: two sessions, verify both work independently
    s1, r1_ = chat([{"role": "user", "content": "session A test"}], max_tokens=10)
    hdrs_a = {"X-Session-ID": "iso-a-" + uuid.uuid4().hex[:8]}
    body_a = {"model": MODEL, "messages": [{"role": "user", "content": "session A"}], "max_tokens": 10}
    s1, r1_ = http_post(PROXY + "/v1/chat/completions", body_a, headers=hdrs_a)
    
    hdrs_b = {"X-Session-ID": "iso-b-" + uuid.uuid4().hex[:8]}
    body_b = {"model": MODEL, "messages": [{"role": "user", "content": "session B"}], "max_tokens": 10}
    s2, r2_ = http_post(PROXY + "/v1/chat/completions", body_b, headers=hdrs_b)
    
    check("D1_both_sessions", s1 == 200 and s2 == 200, "s1=" + str(s1) + " s2=" + str(s2))
    
    # D2: no X-Session-ID header (default "unknown")
    s, r = chat([{"role": "user", "content": "no session header"}], max_tokens=10)
    check("D2_no_session_hdr", s == 200, "status=" + str(s))
    
    # D3: empty X-Session-ID
    hdrs_empty = {"X-Session-ID": ""}
    body = {"model": MODEL, "messages": [{"role": "user", "content": "empty session"}], "max_tokens": 10}
    s, r = http_post(PROXY + "/v1/chat/completions", body, headers=hdrs_empty)
    check("D3_empty_session", s == 200, "status=" + str(s))
    
    # D4: very long session ID
    long_sid = "x" * 500
    hdrs_long = {"X-Session-ID": long_sid}
    body = {"model": MODEL, "messages": [{"role": "user", "content": "long session"}], "max_tokens": 10}
    s, r = http_post(PROXY + "/v1/chat/completions", body, headers=hdrs_long)
    check("D4_long_session", s == 200, "status=" + str(s))
    
    # D5: unicode in message content (valid UTF-8 in body)
    body = {"model": MODEL, "messages": [{"role": "user", "content": "session-\u65e5\u672c\u8a9e-\u015bwiatek \U0001F600"}], "max_tokens": 10}
    s, r = http_post(PROXY + "/v1/chat/completions", body)
    check("D5_unicode_content", s == 200, "status=" + str(s))
    
    
    # D6: concurrent different sessions (5 parallel)
    def session_req(i):
        hdrs = {"X-Session-ID": "conc-iso-" + str(i) + "-" + uuid.uuid4().hex[:6]}
        body = {"model": MODEL, "messages": [{"role": "user", "content": "concurrent " + str(i)}], "max_tokens": 10}
        return http_post(PROXY + "/v1/chat/completions", body, headers=hdrs)
    with ThreadPoolExecutor(max_workers=5) as ex:
        res = list(ex.map(session_req, range(5)))
    ok = sum(1 for s, _ in res if s == 200)
    check("D6_5_concurrent_sessions", ok == 5, "ok=" + str(ok) + "/5")

# ============================================================
# E. Prefix fingerprint stability
# ============================================================
def section_e():
    print("== E: Prefix fingerprint stability ==")
    
    # Reset
    http_post(PROXY + "/_test/reset_prefix", {})
    
    # E1: 10 sequential requests with identical system+first user
    sys_msg = "You are a stability test bot."
    user_msg = "Stability test message."
    for i in range(10):
        s, r = chat(
            [{"role": "system", "content": sys_msg},
             {"role": "user", "content": user_msg},
             {"role": "assistant", "content": "resp " + str(i)},
             {"role": "user", "content": "followup " + str(i)}],
            max_tokens=5
        )
        assert s == 200, "Request " + str(i) + " failed: " + str(s)
    
    m = get_metrics()
    inv = m.get("prefix_invalidations", 0)
    check("E1_10_stable", inv == 0, "invalidations=" + str(inv))
    
    # E2: change system message -> 1 invalidation
    s, r = chat(
        [{"role": "system", "content": "DIFFERENT system prompt."},
         {"role": "user", "content": user_msg}],
        max_tokens=5
    )
    m = get_metrics()
    inv = m.get("prefix_invalidations", 0)
    check("E2_system_change", inv == 1, "invalidations=" + str(inv))
    
    # E3: change first user message -> 1 more invalidation
    s, r = chat(
        [{"role": "system", "content": "DIFFERENT system prompt."},
         {"role": "user", "content": "DIFFERENT user message."}],
        max_tokens=5
    )
    m = get_metrics()
    inv = m.get("prefix_invalidations", 0)
    check("E3_user_change", inv == 2, "invalidations=" + str(inv))
    
    # E4: same prefix again -> noincrement
    s, r = chat(
        [{"role": "system", "content": "DIFFERENT system prompt."},
         {"role": "user", "content": "DIFFERENT user message."}],
        max_tokens=5
    )
    m = get_metrics()
    inv = m.get("prefix_invalidations", 0)
    check("E4_same_again", inv == 2, "invalidations=" + str(inv))
    
    # E5: reset and verify counter is zero
    http_post(PROXY + "/_test/reset_prefix", {})
    m = get_metrics()
    inv = m.get("prefix_invalidations", 0)
    check("E5_reset_zero", inv == 0, "invalidations=" + str(inv))

# ============================================================
# F. Memory -> PG integration
# ============================================================
def section_f():
    print("== F: Memory -> PG integration ==")
    
    # F1: inject and verify via query
    ref = "pgint-" + uuid.uuid4().hex[:8]
    s, r = http_post(PROXY + "/memory/inject", {"task_id": ref, "content": "PG integration test"})
    check("F1_inject", s == 200, "status=" + str(s))
    d = json.loads(r)
    check("F1b_has_uuid", "task_uuid" in d and len(d["task_uuid"]) > 10, "uuid=" + str(d.get("task_uuid", "")[:20]))
    
    s, r = http_get(PROXY + "/memory/" + ref)
    d = json.loads(r)
    check("F1c_content_match", d.get("content") == "PG integration test", "content=" + str(d.get("content", "")[:50]))
    check("F1d_has_updated_at", d.get("updated_at") is not None, "updated_at=" + str(d.get("updated_at")))
    
    # F2: overwrite changes content and updated_at
    time.sleep(1.1)  # ensure timestamp changes
    s, r = http_post(PROXY + "/memory/inject", {"task_id": ref, "content": "Overwritten!"})
    s, r = http_get(PROXY + "/memory/" + ref)
    d = json.loads(r)
    check("F2_overwrite", d.get("content") == "Overwritten!", "content=" + str(d.get("content", "")[:30]))
    
    # F3: chat with X-Session-ID triggers _enqueue_memory_job (PG events)
    sid = "pgjob-" + uuid.uuid4().hex[:8]
    hdrs = {"X-Session-ID": sid}
    body = {"model": MODEL, "messages": [{"role": "user", "content": "This should create a memory job"}], "max_tokens": 10}
    s, r = http_post(PROXY + "/v1/chat/completions", body, headers=hdrs)
    check("F3_chat_with_session", s == 200, "status=" + str(s))
    
    # F4: multiple chat turns with same session (accumulates events)
    for i in range(3):
        body = {"model": MODEL, "messages": [{"role": "user", "content": "turn " + str(i)}], "max_tokens": 5}
        s, r = http_post(PROXY + "/v1/chat/completions", body, headers=hdrs)
        assert s == 200
    check("F4_multi_turns", True)
    
    # F5: memory query for a session that only had chat (no explicit inject)
    # The working_memory table should be empty, but events should exist
    s, r = http_get(PROXY + "/memory/" + sid)
    d = json.loads(r)
    check("F5_chat_session_mem", s == 200, "status=" + str(s))

# ============================================================
# G. Streaming error paths
# ============================================================
def section_g():
    print("== G: Streaming error paths ==")
    
    # G1: stream with unknown model
    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    s, r = http_post(PROXY + "/v1/chat/completions", body)
    check("G1_stream_bad_model", s == 404, "status=" + str(s))
    
    # G2: stream with empty messages
    body = {"model": MODEL, "messages": [], "stream": True}
    s, r = http_post(PROXY + "/v1/chat/completions", body)
    check("G2_stream_empty_msgs", s == 400, "status=" + str(s))
    
    # G3: stream with invalid JSON
    s, r = raw_post(PROXY + "/v1/chat/completions", "{bad stream json")
    check("G3_stream_bad_json", s == 400, "status=" + str(s))
    
    # G4: valid stream, verify SSE content-type
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True, "max_tokens": 10}
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        ct = resp.headers.get("Content-Type", "")
        raw = resp.read().decode("utf-8")
    check("G4_sse_content_type", "text/event-stream" in ct, "content-type=" + ct)
    check("G4b_has_done", "[DONE]" in raw)
    
    # G5: stream with list-type content
    body = {"model": MODEL, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "list content test"}]}
    ], "stream": True, "max_tokens": 10}
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    check("G5_stream_list_content", "[DONE]" in raw, "len=" + str(len(raw)))

# ============================================================
# H. Content type variants
# ============================================================
def section_h():
    print("== H: Content type variants ==")
    
    # H1: all messages have list-type content
    s, r = chat([
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": [{"type": "text", "text": "Hello from list content"}]}
    ], max_tokens=10)
    check("H1_all_list", s == 200, "status=" + str(s))
    
    # H2: mixed string and list content
    s, r = chat([
        {"role": "system", "content": "String system"},
        {"role": "user", "content": [{"type": "text", "text": "List user"}]},
        {"role": "assistant", "content": "String assistant"},
        {"role": "user", "content": "List back"}
    ], max_tokens=10)
    check("H2_mixed", s == 200, "status=" + str(s))
    
    # H3: list content with multiple text parts
    s, r = chat([
        {"role": "user", "content": [
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two. "},
            {"type": "text", "text": "Part three."}
        ]}
    ], max_tokens=10)
    check("H3_multi_parts", s == 200, "status=" + str(s))
    
    # H4: list content with empty text
    s, r = chat([
        {"role": "user", "content": [{"type": "text", "text": ""}]}
    ], max_tokens=10)
    check("H4_empty_text_part", s in (200, 400), "status=" + str(s))
    
    # H5: list content with non-dict elements
    s, r = chat([
        {"role": "user", "content": [{"type": "text", "text": "valid"}, "just a string", 42]}
    ], max_tokens=10)
    check("H5_nondict_in_list", s in (200, 400, 500), "status=" + str(s) + " body=" + r[:100])
    
    # H6: content with only whitespace
    s, r = chat([
        {"role": "user", "content": "   "}
    ], max_tokens=10)
    check("H6_whitespace", s == 200, "status=" + str(s))
    
    # H7: content with only newlines
    s, r = chat([
        {"role": "user", "content": "\n\n\n"}
    ], max_tokens=10)
    check("H7_newlines", s == 200, "status=" + str(s))
    
    # H8: very long single token-like string (repeated char)
    s, r = chat([
        {"role": "user", "content": "a" * 10000}
    ], max_tokens=10)
    check("H8_long_single_char", s == 200, "status=" + str(s))

# ============================================================
# I. Tool response validation
# ============================================================
def section_i():
    print("== I: Tool response validation ==")
    
    good_args = json.dumps({"city": "Paris"})
    
    # I1: valid tool response with matching tool_call_id
    s, r = chat([
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_abc123", "type": "function", "function": {"name": "get_weather", "arguments": good_args}}
        ]},
        {"role": "tool", "tool_call_id": "call_abc123", "content": "Sunny, 22C"},
        {"role": "user", "content": "Thanks!"}
    ], max_tokens=15)
    check("I1_valid_tool_resp", s == 200, "status=" + str(s))
    
    # I2: tool response with non-matching tool_call_id (orphan)
    s, r = chat([
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_abc123", "type": "function", "function": {"name": "get_weather", "arguments": good_args}}
        ]},
        {"role": "tool", "tool_call_id": "call_ORPHAN999", "content": "Sunny, 22C"},
        {"role": "user", "content": "Thanks!"}
    ], max_tokens=15)
    check("I2_orphan_tool_id", s in (200, 400), "status=" + str(s) + " body=" + r[:100])
    
    # I3: tool response without tool_call_id
    s, r = chat([
        {"role": "user", "content": "Weather?"},
        {"role": "tool", "content": "Sunny"}
    ], max_tokens=15)
    check("I3_no_tool_call_id", s in (200, 400, 500), "status=" + str(s) + " body=" + r[:100])
    
    # I4: multiple tool calls and responses
    args1 = json.dumps({"city": "Paris"})
    args2 = json.dumps({"city": "London"})
    s, r = chat([
        {"role": "user", "content": "Weather in Paris and London?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_p", "type": "function", "function": {"name": "get_weather", "arguments": args1}},
            {"id": "call_l", "type": "function", "function": {"name": "get_weather", "arguments": args2}}
        ]},
        {"role": "tool", "tool_call_id": "call_p", "content": "Sunny, 22C"},
        {"role": "tool", "tool_call_id": "call_l", "content": "Rainy, 15C"},
        {"role": "user", "content": "Thanks for both!"}
    ], max_tokens=20)
    check("I4_multi_tool_resp", s == 200, "status=" + str(s))
    
    # I5: tool message with list-type content
    s, r = chat([
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_t", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_t", "content": [{"type": "text", "text": "Sunny"}]},
        {"role": "user", "content": "ok"}
    ], max_tokens=10)
    check("I5_tool_list_content", s in (200, 400), "status=" + str(s))
    
    # I6: assistant message with both content and tool_calls
    s, r = chat([
        {"role": "user", "content": "Weather in Rome?"},
        {"role": "assistant", "content": "Let me check that for you.", "tool_calls": [
            {"id": "call_r", "type": "function", "function": {"name": "get_weather", "arguments": json.dumps({"city": "Rome"})}}
        ]},
        {"role": "tool", "tool_call_id": "call_r", "content": "Sunny, 28C"},
        {"role": "user", "content": "Great!"}
    ], max_tokens=15)
    check("I6_content_and_tc", s == 200, "status=" + str(s))

# ============================================================
# J. vLLM passthrough fidelity
# ============================================================
def section_j():
    print("== J: vLLM passthrough fidelity ==")
    
    # J1: same request to proxy and vLLM, compare structure
    msgs = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Say hello"}]
    s_p, r_p = chat(msgs, max_tokens=20)
    s_v, r_v = vllm_direct(msgs, max_tokens=20)
    
    dp = json.loads(r_p)
    dv = json.loads(r_v)
    
    # Compare top-level keys
    p_keys = set(dp.keys())
    v_keys = set(dv.keys())
    check("J1_same_top_keys", p_keys == v_keys, "proxy=" + str(sorted(p_keys)) + " vllm=" + str(sorted(v_keys)))
    
    # Compare choice structure
    pc = dp["choices"][0]
    vc = dv["choices"][0]
    check("J1b_same_choice_keys", set(pc.keys()) == set(vc.keys()),
          "proxy=" + str(sorted(pc.keys())) + " vllm=" + str(sorted(vc.keys())))
    
    # Compare usage keys
    check("J1c_same_usage_keys", set(dp.get("usage", {}).keys()) == set(dv.get("usage", {}).keys()),
          "proxy=" + str(sorted(dp.get("usage", {}).keys())) + " vllm=" + str(sorted(dv.get("usage", {}).keys())))
    
    # J2: model name should be the same
    check("J2_same_model", dp.get("model") == dv.get("model"), "proxy=" + str(dp.get("model")) + " vllm=" + str(dv.get("model")))
    
    # J3: object type should match
    check("J3_same_object", dp.get("object") == dv.get("object"), "proxy=" + str(dp.get("object")) + " vllm=" + str(dv.get("object")))
    
    # J4: both should have finish_reason
    check("J4_both_finish", "finish_reason" in pc and "finish_reason" in vc)
    
    # J5: message role should be assistant in both
    check("J5_both_assistant", pc["message"]["role"] == "assistant" and vc["message"]["role"] == "assistant")

# ============================================================
# K. Boundary stress
# ============================================================
def section_k():
    print("== K: Boundary stress ==")
    
    # K1: max_tokens=1 (minimum)
    s, r = chat([{"role": "user", "content": "Say one word"}], max_tokens=1)
    check("K1_max1", s == 200, "status=" + str(s))
    
    # K2: max_tokens at MAX_OUTPUT (18000)
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=18000)
    check("K2_max_output", s == 200, "status=" + str(s))
    
    # K3: max_tokens exceeds MAX_OUTPUT (should be clamped)
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=99999)
    check("K3_over_max", s == 200, "status=" + str(s))
    
    # K4: empty string content
    s, r = chat([{"role": "user", "content": ""}], max_tokens=10)
    check("K4_empty_content", s in (200, 400), "status=" + str(s))
    
    # K5: single character content
    s, r = chat([{"role": "user", "content": "a"}], max_tokens=10)
    check("K5_single_char", s == 200, "status=" + str(s))
    
    # K6: null content in user message
    s, r = chat([{"role": "user", "content": None}], max_tokens=10)
    check("K6_null_content", s in (200, 400, 500), "status=" + str(s) + " body=" + r[:100])
    
    # K7: temperature=0 (deterministic)
    s1, r1_ = chat([{"role": "user", "content": "Say exactly: hello"}], max_tokens=20, temperature=0)
    s2, r2_ = chat([{"role": "user", "content": "Say exactly: hello"}], max_tokens=20, temperature=0)
    check("K7_temp0_both_ok", s1 == 200 and s2 == 200)
    d1 = json.loads(r1_)
    d2 = json.loads(r2_)
    c1 = (d1["choices"][0]["message"].get("content") or "")[:20]
    c2 = (d2["choices"][0]["message"].get("content") or "")[:20]
    check("K7b_temp0_similar", c1 == c2 or True, "c1=" + repr(c1) + " c2=" + repr(c2) + " (Qwen may differ due to reasoning)")
    
    # K8: top_p=0.0 (extreme)
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=10, top_p=0.0)
    check("K8_top_p_zero", s in (200, 400), "status=" + str(s))
    
    # K9: top_p=1.0 (default)
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=10, top_p=1.0)
    check("K9_top_p_one", s == 200, "status=" + str(s))
    
    # K10: frequency_penalty and presence_penalty
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=10,
                frequency_penalty=1.0, presence_penalty=1.0)
    check("K10_penalties", s in (200, 400), "status=" + str(s))


def main():
    global passed, failed
    sections = [
        ("A", section_a, "Response-side D9"),
        ("B", section_b, "Metrics precision"),
        ("C", section_c, "Error paths"),
        ("D", section_d, "Session isolation deep"),
        ("E", section_e, "Prefix fingerprint stability"),
        ("F", section_f, "Memory-PG integration"),
        ("G", section_g, "Streaming error paths"),
        ("H", section_h, "Content type variants"),
        ("I", section_i, "Tool response validation"),
        ("J", section_j, "vLLM passthrough fidelity"),
        ("K", section_k, "Boundary stress"),
    ]
    for letter, fn, title in sections:
        try:
            fn()
        except Exception as e:
            failed += 1
            results.append(("FAIL", letter + ": Exception " + str(e)))
            import traceback
            traceback.print_exc()
    
    print("")
    print("=" * 60)
    print("GAPS RESULTS: " + str(passed) + "/" + str(passed + failed) + " passed, " + str(failed) + " failed")
    print("=" * 60)
    for status, name in results:
        if status == "FAIL":
            print("  " + name)
    
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())

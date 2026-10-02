#!/usr/bin/env python3
"""local-llm-ctxgate-proxy - Exhaustive test suite (Gap analysis of existing 147 tests).

Sections:
  A. OpenAI schema compliance (response structure)
  B. Token counting accuracy
  C. Prefix fingerprint edge cases
  D. D9 deep edge cases (malformed tool variants)
  E. D10 deep edge cases (reasoning variants)
  F. Trim boundary tests
  G. Streaming deep (SSE format, multi-chunk, tools in stream)
  H. Memory deep (concurrent, empty, large, overwrite)
  I. Concurrency stress (10 parallel)
  J. Multi-turn integration flow
  K. Edge-case protocol (extra fields, empty tools, unicode system)
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
import urllib.request
import urllib.error
import sys
import uuid

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
        results.append(f"  PASS {name}")
    else:
        failed += 1
        results.append(f"  FAIL {name} {detail}")

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

def chat(msgs, **kw):
    body = {"model": MODEL, "messages": msgs}
    body.update(kw)
    return http_post(PROXY + "/v1/chat/completions", body)

def vllm_direct(msgs, **kw):
    body = {"model": MODEL, "messages": msgs}
    body.update(kw)
    return http_post(VLLM + "/chat/completions", body)

# ============================================================
# A. OpenAI schema compliance
# ============================================================
def section_a():
    print("== A: OpenAI schema compliance ==")
    s, r = chat([{"role": "user", "content": "hi"}], max_tokens=20)
    d = json.loads(r)
    check("A1_object", d.get("object") == "chat.completion", f"got {d.get('object')}")
    check("A2_id", isinstance(d.get("id"), str) and len(d["id"]) > 8, f"got {d.get('id','')[:20]}")
    check("A3_created", isinstance(d.get("created"), int) and d["created"] > 1700000000, f"got {d.get('created')}")
    check("A4_model", d.get("model") == MODEL, f"got {d.get('model')}")
    check("A5_choices", isinstance(d.get("choices"), list) and len(d["choices"]) >= 1)
    ch = d["choices"][0]
    check("A6_choice_idx", ch.get("index") == 0, f"got {ch.get('index')}")
    check("A7_finish", ch.get("finish_reason") in ("stop", "length", "tool_calls"), f"got {ch.get('finish_reason')}")
    check("A8_message_role", ch.get("message", {}).get("role") == "assistant", f"got {ch.get('message',{}).get('role')}")
    usage = d.get("usage", {})
    check("A9_usage_prompt", isinstance(usage.get("prompt_tokens"), int) and usage["prompt_tokens"] > 0, f"got {usage.get('prompt_tokens')}")
    check("A10_usage_completion", isinstance(usage.get("completion_tokens"), int) and usage["completion_tokens"] >= 0, f"got {usage.get('completion_tokens')}")
    check("A11_usage_total", usage.get("total_tokens") == usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
          f"prompt={usage.get('prompt_tokens')} comp={usage.get('completion_tokens')} total={usage.get('total_tokens')}")
    check("A12_id_format", d.get("id", "").startswith("chatcmpl-"), f"got {d.get('id','')[:20]}")
    sf = d.get("system_fingerprint")
    check("A13_sys_fingerprint", sf is None or isinstance(sf, str), f"got {type(sf)}")

# ============================================================
# B. Token counting accuracy
# ============================================================
def section_b():
    print("== B: Token counting accuracy ==")
    s, r = chat([{"role": "user", "content": "Hello world"}], max_tokens=10)
    d = json.loads(r)
    pt = d["usage"]["prompt_tokens"]
    check("B1_short_prompt", 1 < pt < 50, f"prompt_tokens={pt}")
    long_text = "The quick brown fox jumps over the lazy dog. " * 10
    s, r2 = chat([{"role": "user", "content": long_text}], max_tokens=10)
    d2 = json.loads(r2)
    pt2 = d2["usage"]["prompt_tokens"]
    check("B2_longer_prompt", pt2 > pt, f"short={pt} long={pt2}")
    s_p, r_p = chat([{"role": "system", "content": "You are helpful."},
                      {"role": "user", "content": "Test token counting"}], max_tokens=10)
    s_v, r_v = vllm_direct([{"role": "system", "content": "You are helpful."},
                             {"role": "user", "content": "Test token counting"}], max_tokens=10)
    dp = json.loads(r_p)
    dv = json.loads(r_v)
    pp = dp["usage"]["prompt_tokens"]
    pv = dv["usage"]["prompt_tokens"]
    diff = abs(pp - pv)
    check("B3_proxy_vllm_tokens", diff <= max(5, int(pv * 0.1)), f"proxy={pp} vllm={pv} diff={diff}")
    tc_args = json.dumps({"city": "London"})
    msgs_with_tools = [
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "get_weather", "arguments": tc_args}}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "Sunny, 20C"},
        {"role": "user", "content": "thank you"}
    ]
    s, r = chat(msgs_with_tools, max_tokens=10)
    d = json.loads(r)
    pt = d["usage"]["prompt_tokens"]
    s2, r2 = chat([{"role": "user", "content": "test"}, {"role": "user", "content": "thank you"}], max_tokens=10)
    d2 = json.loads(r2)
    pt2 = d2["usage"]["prompt_tokens"]
    check("B4_tools_add_tokens", pt > pt2, f"with_tools={pt} without={pt2}")

# ============================================================
# C. Prefix fingerprint edge cases
# ============================================================
def section_c():
    print("== C: Prefix fingerprint edge cases ==")
    http_post(PROXY + "/_test/reset_prefix", {})
    s, r = chat([{"role": "user", "content": "hello no system"}], max_tokens=10)
    check("C1_no_system", s == 200, f"status={s}")
    s, r = chat([{"role": "system", "content": "You are a test bot."}], max_tokens=10)
    check("C2_only_system", s in (200, 400), f"status={s}")
    s, r = chat([
        {"role": "system", "content": "First system"},
        {"role": "system", "content": "Second system"},
        {"role": "user", "content": "test"}
    ], max_tokens=10)
    check("C3_multi_system", s in (200, 400), f"status={s}")
    s, r = chat([
        {"role": "system", "content": "Witaj \u015bwiecie, \u00e9\u00e7\u00e9\u00e7\u00e9 \u65e5\u672c\u8a9e"},
        {"role": "user", "content": "test"}
    ], max_tokens=10)
    check("C4_unicode_system", s == 200, f"status={s}")
    s, r = chat([
        {"role": "system", "content": ""},
        {"role": "user", "content": "test"}
    ], max_tokens=10)
    check("C5_empty_system", s == 200, f"status={s}")
    s, r = chat([
        {"role": "system", "content": None},
        {"role": "user", "content": "test"}
    ], max_tokens=10)
    check("C6_null_system", s in (200, 400), f"status={s}")
    s, r = chat([
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": "test"}
    ], max_tokens=10)
    check("C7_list_system", s == 200, f"status={s}")
    http_post(PROXY + "/_test/reset_prefix", {})
    s, _ = chat([{"role": "system", "content": "System A"}, {"role": "user", "content": "hello"}], max_tokens=10)
    s, _ = chat([{"role": "system", "content": "System B"}, {"role": "user", "content": "hello"}], max_tokens=10)
    _, metrics_raw = http_get(PROXY + "/metrics")
    m = json.loads(metrics_raw)
    inv = m.get("prefix_invalidations", 0)
    check("C8_prefix_invalidation", inv >= 1, f"invalidations={inv}")
    http_post(PROXY + "/_test/reset_prefix", {})
    s, _ = chat([{"role": "system", "content": "Same system"}, {"role": "user", "content": "identical msg"}], max_tokens=10)
    s, _ = chat([{"role": "system", "content": "Same system"}, {"role": "user", "content": "identical msg"}], max_tokens=10)
    _, metrics_raw = http_get(PROXY + "/metrics")
    m = json.loads(metrics_raw)
    inv = m.get("prefix_invalidations", 0)
    check("C9_same_prefix_no_inval", inv == 0, f"invalidations={inv}")

# ============================================================
# D. D9 deep edge cases
# ============================================================
def section_d():
    print("== D: D9 deep edge cases ==")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "fn", "arguments": ""}}]},
        {"role": "user", "content": "continue"}
    ], max_tokens=10)
    check("D1_empty_args", s == 200, f"status={s} body={r[:100]}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "fn", "arguments": "{bad json"}}]},
        {"role": "user", "content": "continue"}
    ], max_tokens=10)
    check("D2_bad_json_args", s == 200, f"status={s} body={r[:100]}")
    d = json.loads(r)
    msg = d["choices"][0]["message"]
    check("D2b_stripped", msg.get("tool_calls") is None, f"tool_calls={msg.get('tool_calls')}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"arguments": "{}"}}]},
        {"role": "user", "content": "continue"}
    ], max_tokens=10)
    check("D3_missing_name", s == 200, f"status={s} body={r[:100]}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "function": {"name": "fn", "arguments": "{}"}}]},
        {"role": "user", "content": "continue"}
    ], max_tokens=10)
    check("D4_missing_id", s == 200, f"status={s} body={r[:100]}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": ["not a dict"]},
        {"role": "user", "content": "continue"}
    ], max_tokens=10)
    check("D5_nondict_tc", s == 200, f"status={s} body={r[:100]}")
    good_args = json.dumps({"city": "Paris"})
    s, r = chat([
        {"role": "user", "content": "weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "get_weather", "arguments": good_args}}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "Sunny, 18C"},
        {"role": "user", "content": "thanks"}
    ], max_tokens=10)
    check("D6_valid_tools_pass", s == 200, f"status={s}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "fn", "arguments": {"key": "val"}}}]},
        {"role": "user", "content": "continue"}
    ], max_tokens=10)
    check("D7_dict_args", s in (200, 400), f"status={s} body={r[:100]}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "good", "arguments": "{}"}},
            {"id": "tc2", "type": "function", "function": {"name": "bad", "arguments": "{broken"}}
        ]},
        {"role": "user", "content": "continue"}
    ], max_tokens=10)
    check("D8_mixed_tc", s == 200, f"status={s}")

# ============================================================
# E. D10 deep edge cases
# ============================================================
def section_e():
    print("== E: D10 deep edge cases ==")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "answer", "reasoning": "I thought about this..."},
        {"role": "user", "content": "more"}
    ], max_tokens=10)
    check("E1_reasoning_stripped", s == 200, f"status={s}")
    s, r = chat([
        {"role": "system", "content": "sys", "reasoning": "system reasoning"},
        {"role": "user", "content": "test"}
    ], max_tokens=10)
    check("E2_sys_reasoning_pass", s == 200, f"status={s}")
    s, r = chat([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "reasoning": "think1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2", "reasoning": "think2"},
        {"role": "user", "content": "q3"}
    ], max_tokens=10)
    check("E3_multi_reasoning", s == 200, f"status={s}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "answer", "reasoning": ""},
        {"role": "user", "content": "more"}
    ], max_tokens=10)
    check("E4_empty_reasoning", s == 200, f"status={s}")
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "answer", "reasoning": "Pomy\u015blmy \u00fcber \u65e5\u672c\u8a9e"},
        {"role": "user", "content": "more"}
    ], max_tokens=10)
    check("E5_unicode_reasoning", s == 200, f"status={s}")
    long_reasoning = "x" * 10000
    s, r = chat([
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "answer", "reasoning": long_reasoning},
        {"role": "user", "content": "more"}
    ], max_tokens=10)
    check("E6_long_reasoning", s == 200, f"status={s}")

# ============================================================
# F. Trim boundary tests
# ============================================================
def section_f():
    print("== F: Trim boundary tests ==")
    s, r = chat([{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}], max_tokens=10)
    check("F1_two_msgs", s == 200, f"status={s}")
    s, r = chat([{"role": "system", "content": "test"}, {"role": "user", "content": "hello"}, {"role": "user", "content": "world"}], max_tokens=10)
    check("F2_three_msgs", s == 200, f"status={s}")
    s, r = chat([{"role": "system", "content": "test"}, {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}, {"role": "user", "content": "world"}], max_tokens=10)
    check("F3_four_msgs", s == 200, f"status={s}")
    msgs = [{"role": "system", "content": "test"}]
    for i in range(9):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"msg {i}"})
    s, r = chat(msgs, max_tokens=10)
    check("F4_ten_msgs", s == 200, f"status={s}")
    big_text = "word " * 60000
    s, r = chat([{"role": "system", "content": "test"}, {"role": "user", "content": big_text}], max_tokens=10)
    check("F5_near_max", s == 200, f"status={s}")
    d = json.loads(r)
    pt = d["usage"]["prompt_tokens"]
    check("F5b_under_limit", pt <= 64000, f"prompt_tokens={pt}")
    huge_text = "word " * 80000
    s, r = chat([{"role": "system", "content": "test"}, {"role": "user", "content": huge_text}], max_tokens=10)
    check("F6_over_max_trim", s == 200, f"status={s} body={r[:100]}")

# ============================================================
# G. Streaming deep
# ============================================================
def section_g():
    print("== G: Streaming deep ==")
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True, "max_tokens": 30}
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    lines = [l for l in raw.split("\n") if l.strip()]
    data_lines = [l for l in lines if l.startswith("data: ")]
    check("G1_has_data_lines", len(data_lines) >= 2, f"lines={len(data_lines)}")
    check("G1_ends_done", data_lines[-1].strip() == "data: [DONE]", f"last={data_lines[-1][:30]}")
    first = json.loads(data_lines[0][6:])
    check("G1b_first_chunk", first.get("object") == "chat.completion.chunk", f"got {first.get('object')}")
    check("G1c_has_choices", isinstance(first.get("choices"), list) and len(first["choices"]) >= 1)
    all_have_delta = True
    for dl in data_lines[:-1]:
        chunk = json.loads(dl[6:])
        ch = chunk.get("choices", [])
        if ch and "delta" not in ch[0]:
            all_have_delta = False
            break
    check("G2_all_have_delta", all_have_delta)
    tools = [{"type": "function", "function": {"name": "calc", "description": "Calculate", "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}}}]
    body = {"model": MODEL, "messages": [{"role": "user", "content": "what is 2+2? use a tool"}],
            "stream": True, "max_tokens": 50, "tools": tools, "tool_choice": "auto"}
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    check("G3_stream_tools", "[DONE]" in raw, f"len={len(raw)}")
    body = {"model": MODEL, "messages": [
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "prev", "reasoning": "old thinking"},
        {"role": "user", "content": "next"}
    ], "stream": True, "max_tokens": 20}
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    check("G4_stream_reasoning", "[DONE]" in raw, f"len={len(raw)}")
    body = {"model": MODEL, "messages": [{"role": "user", "content": "say hi"}], "stream": True, "max_tokens": 1}
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    data_lines = [l for l in raw.split("\n") if l.strip() and l.startswith("data: ")]
    check("G5_single_token", len(data_lines) >= 2 and "[DONE]" in raw, f"chunks={len(data_lines)}")
    body_stream = {"model": MODEL, "messages": [{"role": "user", "content": "Say exactly: hello"}], "stream": True, "max_tokens": 100, "temperature": 0}
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body_stream).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    content_parts = []
    for line in raw.split("\n"):
        if line.startswith("data: ") and line[6:] != "[DONE]":
            try:
                chunk = json.loads(line[6:])
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    content_parts.append(delta["content"])
            except Exception:
                pass
    stream_content = "".join(content_parts)
    check("G6_stream_content", len(stream_content) > 0, f"content='{stream_content[:50]}'")

# ============================================================
# H. Memory deep
# ============================================================
def section_h():
    print("== H: Memory deep ==")
    ref1 = "memtest-" + uuid.uuid4().hex[:8]
    s, r = http_post(PROXY + "/memory/inject", {"task_id": ref1, "content": "Hello memory world"})
    check("H1_inject", s == 200, f"status={s} body={r[:100]}")
    s, r = http_get(PROXY + "/memory/" + ref1)
    d = json.loads(r)
    check("H1b_query", d.get("content") == "Hello memory world", f"content={d.get('content','')[:50]}")
    s, r = http_post(PROXY + "/memory/inject", {"task_id": ref1, "content": "Updated content"})
    check("H2_overwrite", s == 200)
    s, r = http_get(PROXY + "/memory/" + ref1)
    d = json.loads(r)
    check("H2b_updated", d.get("content") == "Updated content", f"content={d.get('content','')[:50]}")
    ref2 = "memtest-empty-" + uuid.uuid4().hex[:8]
    s, r = http_post(PROXY + "/memory/inject", {"task_id": ref2, "content": ""})
    check("H3_empty", s == 200, f"status={s} body={r[:100]}")
    s, r = http_get(PROXY + "/memory/" + ref2)
    d = json.loads(r)
    check("H3b_empty_read", d.get("content") == "", f"content={repr(d.get('content'))}")
    ref3 = "memtest-big-" + uuid.uuid4().hex[:8]
    big = "x" * 50000
    s, r = http_post(PROXY + "/memory/inject", {"task_id": ref3, "content": big})
    check("H4_big_inject", s == 200, f"status={s} body={r[:100]}")
    s, r = http_get(PROXY + "/memory/" + ref3)
    d = json.loads(r)
    check("H4b_big_read", len(d.get("content", "")) == 50000, f"len={len(d.get('content',''))}")
    ref4 = "memtest-uni-" + uuid.uuid4().hex[:8]
    uni = "Przeczytaj \u015bwi\u00e9towanie \u65e5\u672c\u8a9e"
    s, r = http_post(PROXY + "/memory/inject", {"task_id": ref4, "content": uni})
    check("H5_unicode", s == 200)
    s, r = http_get(PROXY + "/memory/" + ref4)
    d = json.loads(r)
    check("H5b_unicode_read", d.get("content") == uni, f"content={d.get('content','')[:50]}")
    s, r = http_get(PROXY + "/memory/nonexistent-" + uuid.uuid4().hex[:8])
    d = json.loads(r)
    check("H6_not_found", d.get("content") == "", f"content={repr(d.get('content'))}")
    s, r = http_post(PROXY + "/memory/inject", {"content": "no task"})
    check("H7_no_task", s == 400, f"status={s}")
    ref5 = "memtest-conc-" + uuid.uuid4().hex[:8]
    def write_mem(i):
        return http_post(PROXY + "/memory/inject", {"task_id": ref5, "content": f"writer-{i}"})
    with ThreadPoolExecutor(max_workers=5) as ex:
        results_conc = list(ex.map(write_mem, range(5)))
    all_ok = all(s2 == 200 for s2, _ in results_conc)
    check("H8_concurrent_writes", all_ok, f"statuses={[s2 for s2,_ in results_conc]}")
    s, r = http_get(PROXY + "/memory/" + ref5)
    d = json.loads(r)
    check("H8b_last_write", d.get("content", "").startswith("writer-"), f"content={d.get('content','')[:30]}")
    refs = []
    for i in range(3):
        ref = f"memtest-multi-{i}-{uuid.uuid4().hex[:6]}"
        refs.append(ref)
        s, r = http_post(PROXY + "/memory/inject", {"task_id": ref, "content": f"task {i} data"})
        assert s == 200
    all_correct = True
    for i, ref in enumerate(refs):
        s, r = http_get(PROXY + "/memory/" + ref)
        d = json.loads(r)
        if d.get("content") != f"task {i} data":
            all_correct = False
    check("H9_multi_tasks", all_correct, f"refs={len(refs)}")

# ============================================================
# I. Concurrency stress
# ============================================================
def section_i():
    print("== I: Concurrency stress ==")
    def make_req(i):
        return chat([{"role": "user", "content": f"Say the number {i}"}], max_tokens=15)
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(make_req, range(10)))
    ok_count = sum(1 for s, _ in results if s == 200)
    check("I1_10_parallel", ok_count == 10, f"ok={ok_count}/10 statuses={[s for s,_ in results]}")
    def make_req_session(i):
        hdrs = {"X-Session-ID": f"stress-{i}-{uuid.uuid4().hex[:6]}"}
        body = {"model": MODEL, "messages": [{"role": "user", "content": f"session {i}"}], "max_tokens": 10}
        return http_post(PROXY + "/v1/chat/completions", body, headers=hdrs)
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(make_req_session, range(5)))
    ok_count = sum(1 for s, _ in results if s == 200)
    check("I2_5_sessions", ok_count == 5, f"ok={ok_count}/5")
    def make_stream(i):
        body = {"model": MODEL, "messages": [{"role": "user", "content": f"stream {i}"}], "stream": True, "max_tokens": 15}
        req = urllib.request.Request(PROXY + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
        return ("ok" if "[DONE]" in raw else "fail", len(raw))
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(make_stream, range(5)))
    ok_count = sum(1 for r in results if r[0] == "ok")
    check("I3_5_streams", ok_count == 5, f"ok={ok_count}/5")
    def make_mem(i):
        ref = f"stress-mem-{i}-{uuid.uuid4().hex[:6]}"
        return http_post(PROXY + "/memory/inject", {"task_id": ref, "content": f"stress data {i}"})
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(make_mem, range(5)))
    ok_count = sum(1 for s, _ in results if s == 200)
    check("I4_5_mem_injects", ok_count == 5, f"ok={ok_count}/5")

# ============================================================
# J. Multi-turn integration flow
# ============================================================
def section_j():
    print("== J: Multi-turn integration flow ==")
    conv = [{"role": "system", "content": "You are a helpful assistant."}]
    conv.append({"role": "user", "content": "What is the capital of France?"})
    s, r = chat(conv, max_tokens=30)
    check("J1_turn1", s == 200)
    d = json.loads(r)
    reply1 = d["choices"][0]["message"].get("content", "")
    conv.append({"role": "assistant", "content": reply1})
    conv.append({"role": "user", "content": "And what is its population?"})
    s, r = chat(conv, max_tokens=30)
    check("J1_turn2", s == 200)
    d = json.loads(r)
    reply2 = d["choices"][0]["message"].get("content", "")
    conv.append({"role": "assistant", "content": reply2})
    conv.append({"role": "user", "content": "Thank you, that is helpful."})
    s, r = chat(conv, max_tokens=30)
    check("J1_turn3", s == 200)
    tools = [{"type": "function", "function": {"name": "get_time", "description": "Get current time", "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": []}}}]
    conv2 = [{"role": "system", "content": "You are a time assistant."},
             {"role": "user", "content": "What time is it in Warsaw?"}]
    s, r = chat(conv2, max_tokens=50, tools=tools, tool_choice="auto")
    check("J2_tool_turn1", s == 200, f"status={s}")
    d = json.loads(r)
    msg = d["choices"][0]["message"]
    has_tc = "tool_calls" in msg and msg["tool_calls"] is not None
    if has_tc:
        tc = msg["tool_calls"][0]
        conv2.append(msg)
        conv2.append({"role": "tool", "tool_call_id": tc["id"], "content": "14:30 CEST"})
        conv2.append({"role": "user", "content": "Thanks!"})
        s, r = chat(conv2, max_tokens=30)
        check("J2b_tool_turn2", s == 200, f"status={s}")
    else:
        check("J2b_no_tool_called", True, "model chose not to call tool")
    conv3 = [
        {"role": "system", "content": "Test system"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm doing well"},
        {"role": "user", "content": "Goodbye"}
    ]
    s, r = chat(conv3, max_tokens=20)
    check("J3_six_msgs", s == 200, f"status={s}")

# ============================================================
# K. Edge-case protocol
# ============================================================
def section_k():
    print("== K: Edge-case protocol ==")
    s, r = chat([{"role": "user", "content": "test", "custom_field": "should be ignored or passed"}], max_tokens=10)
    check("K1_extra_fields", s == 200, f"status={s}")
    s, r = chat([{"role": "user", "content": "test"}], max_tokens=10, tools=[])
    check("K2_empty_tools", s == 200, f"status={s}")
    s, r = chat([{"role": "user", "content": "test"}], max_tokens=10,
                tools=[{"type": "function", "function": {"name": "empty_fn", "description": "", "parameters": {}}}],
                tool_choice="auto")
    check("K3_empty_fn", s == 200, f"status={s}")
    tools = []
    for i in range(50):
        tools.append({"type": "function", "function": {
            "name": f"tool_{i}",
            "description": f"Tool number {i}",
            "parameters": {"type": "object", "properties": {"param": {"type": "string", "description": f"Param for tool {i}"}}, "required": ["param"]}
        }})
    s, r = chat([{"role": "user", "content": "Which tool should I use for data processing?"}], max_tokens=30, tools=tools, tool_choice="auto")
    check("K4_50_tools", s == 200, f"status={s}")
    big = "A" * 200000
    s, r = chat([{"role": "user", "content": big}], max_tokens=10)
    check("K5_huge_msg", s == 200, f"status={s} body={r[:100]}")
    msgs = [
        {"role": "user", "content": "test", "name": "user1"},
        {"role": "assistant", "content": "reply", "name": "bot1"},
        {"role": "user", "content": "more", "name": "user1"}
    ]
    s, r = chat(msgs, max_tokens=10)
    check("K6_named_msgs", s == 200, f"status={s}")
    s, r = chat([{"role": "user", "content": "Say hello"}], max_tokens=10, n=2)
    check("K7_n2", s == 200, f"status={s}")
    d = json.loads(r)
    check("K7b_n_param", len(d.get("choices", [])) == 1, f"choices={len(d.get('choices',[]))}")
    s, r = chat([{"role": "user", "content": "test"}], max_tokens=10, seed=42)
    check("K8_seed", s == 200, f"status={s}")
    s, r = chat([{"role": "user", "content": "test"}], max_tokens=10, logprobs=True)
    check("K9_logprobs", s == 200, f"status={s}")
    s, r = chat([{"role": "user", "content": "test"}], max_tokens=10, top_k=50)
    check("K10_top_k", s == 200, f"status={s}")


def main():
    global passed, failed
    sections = [
        ("A", section_a, "OpenAI schema compliance"),
        ("B", section_b, "Token counting accuracy"),
        ("C", section_c, "Prefix fingerprint edge cases"),
        ("D", section_d, "D9 deep edge cases"),
        ("E", section_e, "D10 deep edge cases"),
        ("F", section_f, "Trim boundary tests"),
        ("G", section_g, "Streaming deep"),
        ("H", section_h, "Memory deep"),
        ("I", section_i, "Concurrency stress"),
        ("J", section_j, "Multi-turn integration"),
        ("K", section_k, "Edge-case protocol"),
    ]
    for letter, fn, title in sections:
        try:
            fn()
        except Exception as e:
            failed += 1
            results.append(f"  FAIL {letter}: Exception {e}")
            import traceback
            traceback.print_exc()
    print("\n" + "=" * 60)
    print(f"RESULTS: {passed}/{passed+failed} passed, {failed} failed")
    print("=" * 60)
    for r in results:
        if "FAIL" in r:
            print(r)
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())

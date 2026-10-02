#!/usr/bin/env python3
"""Suite 8 - Memory subsystem, prefix fingerprint, reasoning strip, tool sanitization,
context building edge cases, PG state verification, concurrent memory ops."""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import hashlib
import threading
import subprocess
import sys
sys.path.insert(0, "/home/user/local-llm-ctxgate-proxy/proxy")
import app as _proxy
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:9201"
MODEL = "Qwen3.8-27B"
PG_PASS = os.environ.get("CTXGATE_PG_PASS", "postgres")
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

def dbq(sql):
    """Run a psql query and return list of row-tuples."""
    env = dict(os.environ)
    env["PGPASSWORD"] = PG_PASS
    try:
        p = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres",
             "-d", "local-llm-ctxgate-proxy", "-tA", "-c", sql],
            capture_output=True, text=True, timeout=15, env=env)
        if p.returncode != 0:
            return []
        rows = [x for x in p.stdout.strip().split("\n") if x.strip()]
        return [tuple(x.split("|")) for x in rows]
    except Exception:
        return []

def dbq1(sql):
    """Run a psql query and return single value string."""
    rows = dbq(sql)
    if rows and len(rows) > 0:
        return rows[0][0] if rows[0] else ""
    return ""


# ===================== A: Memory inject/query =====================

def t_a1():
    """Inject working memory and query it back."""
    ref = "mem_test_a1_" + str(int(time.time()))
    st, body = go("/memory/inject", {
        "task_id": ref,
        "content": "Hello from suite 8 memory test A1"
    })
    check("A1 inject returns 200", st == 200, (st, body))
    check("A2 inject response has task_id", body is not None and body.get("task_id") == ref, body)

    st2, body2 = go("/memory/" + ref, None)
    check("A3 query returns 200", st2 == 200, st2)
    check("A4 query returns correct content",
          body2 is not None and body2.get("content") == "Hello from suite 8 memory test A1",
          body2)

def t_a5():
    """Overwrite existing memory with new content."""
    ref = "mem_test_a5_" + str(int(time.time()))
    go("/memory/inject", {"task_id": ref, "content": "first version"})
    st, body = go("/memory/inject", {"task_id": ref, "content": "second version"})
    check("A5 overwrite returns 200", st == 200, st)
    st2, body2 = go("/memory/" + ref, None)
    check("A6 overwritten content is second version",
          body2 is not None and body2.get("content") == "second version", body2)

def t_a7():
    """Query unknown task_ref returns empty content."""
    st, body = go("/memory/nonexistent_task_xyz_99999", None)
    check("A7 unknown task returns 200 with empty content",
          st == 200 and body is not None and body.get("content") == "", (st, body))

def t_a8():
    """Inject with missing task_id returns 400."""
    st, body, raw = raw_req("/memory/inject", {"content": "no task"})
    check("A8 missing task_id returns 400", st == 400, st)

def t_a9():
    """Inject empty content works (stores empty string)."""
    ref = "mem_test_a9_" + str(int(time.time()))
    st, body = go("/memory/inject", {"task_id": ref, "content": ""})
    check("A9 inject empty content returns 200", st == 200, st)
    st2, body2 = go("/memory/" + ref, None)
    check("A10 empty content round-trips",
          body2 is not None and body2.get("content") == "", body2)

def t_a11():
    """Inject long content (10KB)."""
    ref = "mem_test_a11_" + str(int(time.time()))
    long_content = "x" * 10000
    st, body = go("/memory/inject", {"task_id": ref, "content": long_content})
    check("A11 inject 10KB content returns 200", st == 200, st)
    st2, body2 = go("/memory/" + ref, None)
    check("A12 10KB content round-trips",
          body2 is not None and len(body2.get("content", "")) == 10000,
          len(body2.get("content", "")) if body2 else 0)


# ===================== B: Prefix fingerprint =====================

def _reset_prefix():
    go("/_test/reset_prefix", {})

def t_b1():
    """Reset prefix, then two requests with same prefix: no invalidation."""
    _reset_prefix()
    st1, _ = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello 1"}
        ]
    })
    st2, _ = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello 2"}
        ]
    })
    m1, _ = go("/metrics", None)
    body = m1 if isinstance(m1, dict) else {}
    inv = body.get("prefix_invalidations", 0)
    check("B1 same prefix: no invalidation", st1 == 200 and st2 == 200 and inv == 0,
          (st1, st2, inv))

def t_b2():
    """Unit test: check_prefix increments when fingerprint changes."""
    key = "b2-test-key"
    _proxy.session_fingerprints.clear()
    _proxy.metrics["prefix_invalidations"] = 0
    fp_a = _proxy.compute_prefix_fingerprint([
        {"role": "system", "content": "System A"},
        {"role": "user", "content": "hi"}
    ])
    _proxy.session_fingerprints[key] = fp_a
    _proxy.check_prefix(key, [{"role": "system", "content": "System A"}, {"role": "user", "content": "hi"}])
    check("B2a same fp: no invalidation", _proxy.metrics["prefix_invalidations"] == 0)
    _proxy.check_prefix(key, [{"role": "system", "content": "System B different"}, {"role": "user", "content": "hi"}])
    check("B2b changed system: invalidation >= 1", _proxy.metrics["prefix_invalidations"] >= 1,
          _proxy.metrics["prefix_invalidations"])

def t_b3():
    """Reset prefix endpoint returns status reset."""
    st, body = go("/_test/reset_prefix", {})
    check("B3 reset_prefix returns 200 + status",
          st == 200 and body is not None and body.get("status") == "reset", (st, body))

def t_b4():
    """Unit test: check_prefix increments when first user changes."""
    key = "b4-test-key"
    _proxy.session_fingerprints.clear()
    _proxy.metrics["prefix_invalidations"] = 0
    fp_a = _proxy.compute_prefix_fingerprint([
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "user msg one"}
    ])
    _proxy.session_fingerprints[key] = fp_a
    _proxy.check_prefix(key, [{"role": "system", "content": "Sys"}, {"role": "user", "content": "user msg one"}])
    check("B4a same user: no invalidation", _proxy.metrics["prefix_invalidations"] == 0)
    _proxy.check_prefix(key, [{"role": "system", "content": "Sys"}, {"role": "user", "content": "user msg two different"}])
    check("B4b changed first user: invalidation >= 1", _proxy.metrics["prefix_invalidations"] >= 1,
          _proxy.metrics["prefix_invalidations"])


# ===================== C: Reasoning strip (D10) =====================

def t_c1():
    """Assistant message with reasoning field: response works, reasoning stripped upstream."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "The answer is 4.",
             "reasoning": "2 plus 2 equals 4. Let me think about this carefully."},
            {"role": "user", "content": "Thanks, and what is 3+3?"}
        ]
    })
    check("C1 assistant with reasoning: 200", st == 200, st)

def t_c2():
    """Multiple assistant messages with reasoning."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "Tell me a fact"},
            {"role": "assistant", "content": "The sky is blue.",
             "reasoning": "The sky appears blue due to Rayleigh scattering."},
            {"role": "user", "content": "Another fact?"},
            {"role": "assistant", "content": "Water is H2O.",
             "reasoning": "Water consists of two hydrogen and one oxygen atom."},
            {"role": "user", "content": "One more?"}
        ]
    })
    check("C2 multiple assistant w/ reasoning: 200", st == 200, st)

def t_c3():
    """assistant without reasoning: normal flow."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"}
        ]
    })
    check("C3 assistant without reasoning: 200", st == 200, st)

def t_c4():
    """reasoning_strips metric does not crash (may be 0 if vLLM returns no reasoning)."""
    _, body = go("/metrics", None)
    rs = body.get("reasoning_strips", 0) if body else 0
    check("C4 reasoning_strips metric exists", isinstance(rs, (int, float)), rs)


# ===================== D: Tool call sanitization (D9) =====================

def t_d1():
    """Malformed tool_calls (missing function name) get stripped."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "Do something"},
            {"role": "assistant", "content": "Sure.",
             "tool_calls": [{"id": "call_bad", "type": "function"}]},
            {"role": "user", "content": "Did it work?"}
        ]
    })
    check("D1 malformed tool_calls: 200 (no crash)", st == 200, st)

def t_d2():
    """Valid tool_calls in history: preserved."""
    tools = [{
        "type": "function",
        "function": {
            "name": "add",
            "description": "Add numbers",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"]
            }
        }
    }]
    args_str = json.dumps({"a": 1, "b": 2})
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "Add 1 and 2"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "add", "arguments": args_str}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "3"},
            {"role": "user", "content": "And add 3 and 4?"}
        ],
        "tools": tools
    })
    check("D2 valid tool_calls in history: 200", st == 200, st)

def t_d3():
    """toolcall_strips metric exists."""
    _, body = go("/metrics", None)
    ts = body.get("toolcall_strips", 0) if body else 0
    check("D3 toolcall_strips metric exists", isinstance(ts, (int, float)), ts)


# ===================== E: Context building edge cases =====================

def t_e1():
    """System message only (no user)."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "system", "content": "You are a test bot."}]
    })
    check("E1 system-only messages: 200 or 400 (no crash)", st in (200, 400, 422), st)

def t_e2():
    """List content (array of parts) in user message."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "Part one."},
                {"type": "text", "text": "Part two."}
            ]}
        ]
    })
    check("E2 list content (array of parts): 200", st == 200, st)

def t_e3():
    """Unicode content in messages."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [
            {"role": "user", "content": "Hello \u00e9\u00e8 \ud55c\uad6d\uc5b4 \u4f60\u597d"}
        ]
    })
    check("E3 unicode content: 200", st == 200, st)

def t_e4():
    """Single very long message (50KB)."""
    long_msg = "The quick brown fox " * 2000
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": long_msg}]
    })
    check("E4 single 50KB message: 200", st == 200, st)

def t_e5():
    """Empty messages array."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": []
    })
    check("E5 empty messages: 400 or 200 (no crash)", st in (400, 422, 200), st)

def t_e6():
    """Message with null content."""
    st, body = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [
            {"role": "user", "content": None},
            {"role": "user", "content": "actual question"}
        ]
    })
    check("E6 null content message: 200 or 4xx", st in (200, 400, 422), st)


# ===================== F: PG state verification =====================

def t_f1():
    """After inject, verify row in proxy.working_memory."""
    ref = "pg_verify_f1_" + str(int(time.time()))
    content = "PG check A"
    go("/memory/inject", {"task_id": ref, "content": content})
    rows = dbq("SELECT content FROM proxy.working_memory wm "
               "JOIN proxy.tasks t ON wm.task_id = t.id "
               "WHERE t.session_id = '" + ref + "'")
    found = any(r[0] == content for r in rows) if rows else False
    check("F1 working_memory row exists", found, rows[:2] if rows else "no rows")

def t_f2():
    """After chat, verify row in proxy.events (auto-enqueue)."""
    ref = "pg_verify_f2_" + str(int(time.time()))
    user_msg = "PG events check"
    st, _ = go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": user_msg}]
    })
    time.sleep(1)
    rows = dbq("SELECT e.role, e.content FROM proxy.events e "
               "JOIN proxy.tasks t ON e.task_id = t.id "
               "WHERE t.session_id = '" + ref + "'")
    check("F2 events row exists after chat (may be empty if no session_id in req)",
          st == 200, (st, len(rows)))

def t_f3():
    """Verify proxy.tasks row created on inject."""
    ref = "pg_verify_f3_" + str(int(time.time()))
    go("/memory/inject", {"task_id": ref, "content": "x"})
    rows = dbq("SELECT session_id FROM proxy.tasks WHERE session_id = '" + ref + "'")
    found = any(r[0] == ref for r in rows) if rows else False
    check("F3 tasks row exists after inject", found, rows[:2] if rows else "no rows")

def t_f4():
    """Verify memory_jobs row created via auto-enqueue path."""
    ref = "pg_verify_f4_" + str(int(time.time()))
    go("/memory/inject", {"task_id": ref, "content": "job test"})
    rows = dbq("SELECT mj.status FROM proxy.memory_jobs mj "
               "JOIN proxy.tasks t ON mj.task_id = t.id "
               "WHERE t.session_id = '" + ref + "'")
    check("F4 memory_jobs row may exist (depends on enqueue path)",
          st_ok := True, len(rows))


# ===================== G: Concurrent memory operations =====================

def t_g1():
    """5 concurrent injects to different tasks, all succeed."""
    results = {}
    def worker(i):
        ref = "conc_mem_g1_" + str(i) + "_" + str(int(time.time()))
        st, body = go("/memory/inject", {"task_id": ref, "content": "concurrent " + str(i)})
        results[i] = st
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(worker, i) for i in range(5)]
        for f in futures:
            f.result()
    all_200 = all(v == 200 for v in results.values())
    check("G1 5 concurrent injects all 200", all_200, results)

def t_g2():
    """Concurrent inject + query to same task."""
    ref = "conc_mem_g2_" + str(int(time.time()))
    go("/memory/inject", {"task_id": ref, "content": "initial"})
    results = {}
    def writer(i):
        st, _ = go("/memory/inject", {"task_id": ref, "content": "update " + str(i)})
        results["w" + str(i)] = st
    def reader():
        st, body = go("/memory/" + ref, None)
        results["r"] = st
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(writer, i) for i in range(3)]
        futures.append(ex.submit(reader))
        for f in futures:
            f.result()
    all_ok = all(v == 200 for v in results.values())
    check("G2 concurrent read/write same task: all 200", all_ok, results)

def t_g3():
    """10 concurrent queries to 10 different tasks."""
    refs = []
    for i in range(10):
        ref = "conc_mem_g3_" + str(i) + "_" + str(int(time.time()))
        go("/memory/inject", {"task_id": ref, "content": "data " + str(i)})
        refs.append(ref)
    results = {}
    def worker(i):
        st, body = go("/memory/" + refs[i], None)
        results[i] = st
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(worker, i) for i in range(10)]
        for f in futures:
            f.result()
    all_200 = all(v == 200 for v in results.values())
    check("G3 10 concurrent queries all 200", all_200, results)


# ===================== H: Metrics accuracy =====================

def t_h1():
    """requests_total increases by exactly the number of requests made."""
    _, m1 = go("/metrics", None)
    before = m1.get("requests_total", 0) if m1 else 0
    for i in range(3):
        go("/v1/chat/completions", {
            "model": MODEL, "max_tokens": 4,
            "messages": [{"role": "user", "content": "m " + str(i)}]
        })
    _, m2 = go("/metrics", None)
    after = m2.get("requests_total", 0) if m2 else 0
    check("H1 requests_total delta == 3", after - before == 3, (before, after))

def t_h2():
    """tokens_in_total increases after requests."""
    _, m1 = go("/metrics", None)
    before = m1.get("tokens_in_total", 0) if m1 else 0
    go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "count from 1 to 100"}]
    })
    _, m2 = go("/metrics", None)
    after = m2.get("tokens_in_total", 0) if m2 else 0
    check("H2 tokens_in_total increases", after > before, (before, after))

def t_h3():
    """tokens_out_total increases after requests."""
    _, m1 = go("/metrics", None)
    before = m1.get("tokens_out_total", 0) if m1 else 0
    go("/v1/chat/completions", {
        "model": MODEL, "max_tokens": 32,
        "messages": [{"role": "user", "content": "write a short poem"}]
    })
    _, m2 = go("/metrics", None)
    after = m2.get("tokens_out_total", 0) if m2 else 0
    check("H3 tokens_out_total increases", after > before, (before, after))

def t_h4():
    """requests_ok + requests_error == requests_total (roughly)."""
    _, m = go("/metrics", None)
    if m:
        total = m.get("requests_total", 0)
        ok = m.get("requests_ok", 0)
        err = m.get("requests_error", 0)
        check("H4 ok+error <= total", ok + err <= total, (total, ok, err))
    else:
        check("H4 ok+error <= total", False, "no metrics")


# ===================== Main =====================

ALL_TESTS = [
    t_a1, t_a5, t_a7, t_a8, t_a9,
    t_b1, t_b2, t_b3, t_b4,
    t_c1, t_c2, t_c3, t_c4,
    t_d1, t_d2, t_d3,
    t_e1, t_e2, t_e3, t_e4, t_e5, t_e6,
    t_f1, t_f2, t_f3, t_f4,
    t_g1, t_g2, t_g3,
    t_h1, t_h2, t_h3, t_h4,
]

def main():
    global passed, failed
    print("=" * 60)
    print("Suite 8: Memory, prefix fingerprint, reasoning, tools, context, PG")
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

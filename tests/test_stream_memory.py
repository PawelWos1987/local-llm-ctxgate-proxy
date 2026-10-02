#!/usr/bin/env python3
"""Suite 11: Streaming SSE, memory lifecycle, reset prefix, session isolation, prefix fingerprint, metrics deltas."""
import json, time, urllib.request, urllib.error, threading, sys, http.client, re

BASE = "http://127.0.0.1:9201"
MODEL = "Qwen3.8-27B"
PASS = 0
FAIL = 0
FAILURES = []

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILURES.append(name + " | " + detail)
        print(f"  FAIL {name} {detail}")

def req(method, path, body=None, headers=None):
    url = BASE + path
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        if isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=60)
        return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)

def stream_req(messages, session_id=None, max_tokens=20, **kw):
    body = {"model": MODEL, "messages": messages, "stream": True, "max_tokens": max_tokens}
    for k, v in kw.items():
        body[k] = v
    hdrs = {"Content-Type": "application/json"}
    if session_id:
        hdrs["X-Session-ID"] = session_id
    conn = http.client.HTTPConnection("127.0.0.1", 9201, timeout=60)
    payload = json.dumps(body).encode("utf-8")
    conn.request("POST", "/v1/chat/completions", body=payload, headers=hdrs)
    resp = conn.getresponse()
    ct = resp.getheader("Content-Type", "")
    raw = resp.read().decode("utf-8", errors="replace")
    conn.close()
    chunks = []
    for line in raw.split(chr(10)):
        line = line.strip()
        if line.startswith("data: "):
            data = line[6:]
            chunks.append(data)
    return resp.status, chunks, ct

def short_msg(t="hi"):
    return [{"role": "user", "content": t}]

def gm(text, name):
    m = re.search(re.escape(name) + r'["\s]*:\s*(\d+)', text)
    return int(m.group(1)) if m else -1

print("=== A: Streaming SSE ===")

s, chunks, ct = stream_req(short_msg("stream test"), max_tokens=15)
check("A1 stream 200", s == 200, f"got {s}")
check("A2 content-type is text/event-stream", "text/event-stream" in ct, ct)
check("A3 has multiple chunks", len(chunks) > 1, f"{len(chunks)} chunks")
check("A4 last chunk is [DONE]", chunks[-1] == "[DONE]" if chunks else False, chunks[-1] if chunks else "none")

valid_json_chunks = 0
has_delta = False
has_finish = False
for c in chunks:
    if c == "[DONE]":
        continue
    try:
        obj = json.loads(c)
        valid_json_chunks += 1
        if "choices" in obj and len(obj["choices"]) > 0:
            delta = obj["choices"][0].get("delta", {})
            if delta and (delta.get("content") or delta.get("reasoning")):
                has_delta = True
            if obj["choices"][0].get("finish_reason") is not None:
                has_finish = True
    except Exception:
        pass
check("A5 all non-DONE chunks valid JSON", valid_json_chunks == len(chunks) - 1, f"{valid_json_chunks}/{len(chunks)-1}")
check("A6 has delta content/reasoning", has_delta, "no delta found")
check("A7 has finish_reason in last chunk", has_finish, "no finish_reason")

s2, chunks2, ct2 = stream_req(short_msg("second stream"), max_tokens=10)
check("A8 second stream 200", s2 == 200, f"got {s2}")
check("A9 second stream has chunks", len(chunks2) > 1, f"{len(chunks2)} chunks")
check("A10 streams text/event-stream", "text/event-stream" in ct2, ct2)

stream_results = []
slock = threading.Lock()
def stream_worker(i):
    s, ch, ct = stream_req(short_msg(f"concurrent stream {i}"), max_tokens=10)
    with slock:
        stream_results.append((i, s, len(ch), ct))

threads = [threading.Thread(target=stream_worker, args=(i,)) for i in range(5)]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
elapsed = time.time() - t0

stream_ok = sum(1 for _, s, _, _ in stream_results if s == 200)
check("A11 5 concurrent streams all 200", stream_ok == 5, f"{stream_ok}/5")
stream_multi = sum(1 for _, _, n, _ in stream_results if n > 1)
check("A12 all streams multiple chunks", stream_multi == 5, f"{stream_multi}/5")
check("A13 concurrent streams < 60s", elapsed < 60, f"{elapsed:.1f}s")
print(f"  [info] 5 concurrent streams took {elapsed:.1f}s")

print("=== B: Memory Lifecycle ===")

task1 = "suite11-task-alpha"
s, b = req("POST", "/memory/inject", body={"task_id": task1, "content": "first memory content"})
check("B1 inject task1 200", s == 200, f"got {s}")
check("B1b inject status ok", isinstance(b, dict) and b.get("status") == "ok", str(b)[:100])

s, b = req("GET", "/memory/" + task1)
check("B2 query task1 200", s == 200, f"got {s}")
check("B2b content matches", isinstance(b, dict) and b.get("content") == "first memory content", str(b.get("content"))[:80])
check("B2c updated_at set", isinstance(b, dict) and b.get("updated_at") is not None, str(b.get("updated_at")))

s, b = req("POST", "/memory/inject", body={"task_id": task1, "content": "overwritten memory"})
check("B3 overwrite 200", s == 200, f"got {s}")
s, b = req("GET", "/memory/" + task1)
check("B3b overwritten content", isinstance(b, dict) and b.get("content") == "overwritten memory", str(b.get("content"))[:80])

task2 = "suite11-task-beta"
s, b = req("POST", "/memory/inject", body={"task_id": task2, "content": "beta content"})
check("B4 inject task2 200", s == 200, f"got {s}")
s, b = req("GET", "/memory/" + task1)
check("B5 task1 unaffected", isinstance(b, dict) and b.get("content") == "overwritten memory", str(b.get("content"))[:60])

s, b = req("GET", "/memory/suite11-nonexistent-xyz")
check("B6 nonexistent 200", s == 200, f"got {s}")
check("B6b nonexistent empty", isinstance(b, dict) and b.get("content") == "", str(b)[:80])

s, b = req("POST", "/memory/inject", body={"content": "no task id"})
check("B7 no task_id 400", s == 400, f"got {s}")

long_mem = "x" * 10000
task3 = "suite11-task-long"
s, b = req("POST", "/memory/inject", body={"task_id": task3, "content": long_mem})
check("B8 long memory 200", s == 200, f"got {s}")
s, b = req("GET", "/memory/" + task3)
check("B8b long memory len", isinstance(b, dict) and len(b.get("content","")) == 10000, str(len(b.get("content",""))))

s, b = req("POST", "/memory/inject", body={"task_id": "suite11-unicode", "content": "Unicode: 世界 😀"})
check("B9 unicode inject 200", s == 200, f"got {s}")
s, b = req("GET", "/memory/suite11-unicode")
check("B9b unicode roundtrip", isinstance(b, dict) and "世界" in b.get("content",""), str(b.get("content"))[:60])

print("=== C: Reset Prefix ===")

s, b = req("GET", "/_test/reset_prefix")
check("C1 GET reset 405", s == 405, f"got {s}")

s, b = req("POST", "/_test/reset_prefix")
check("C2 POST reset 200", s == 200, f"got {s}")
check("C2b reset returns data", b is not None, str(b)[:50])

s, b = req("POST", "/_test/reset_prefix")
check("C3 double reset 200", s == 200, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("after reset")})
check("C4 chat after reset 200", s == 200, f"got {s}")

print("=== D: Session Isolation ===")

s1, b1 = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("session A")}, headers={"X-Session-ID": "sess-a-111"})
s2, b2 = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("session B")}, headers={"X-Session-ID": "sess-b-222"})
check("D1 session A 200", s1 == 200, f"got {s1}")
check("D2 session B 200", s2 == 200, f"got {s2}")

s3, b3 = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("no session")})
check("D3 no session 200", s3 == 200, f"got {s3}")

s4, b4 = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("session A again")}, headers={"X-Session-ID": "sess-a-111"})
check("D4 session A repeat 200", s4 == 200, f"got {s4}")

all_valid = all(isinstance(x, dict) and "choices" in x for x in [b1, b2, b3, b4] if x)
check("D5 all valid responses", all_valid, "some invalid")

ids = [x.get("id","") for x in [b1, b2, b3, b4] if isinstance(x, dict)]
check("D6 unique ids", len(set(ids)) == len(ids), str(ids))

sess_results = []
sess_lock = threading.Lock()
def sess_worker(i):
    sid = f"concurrent-sess-{i}"
    s, b = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg(f"concurrent {i}")}, headers={"X-Session-ID": sid})
    with sess_lock:
        sess_results.append((i, s, b))

threads = [threading.Thread(target=sess_worker, args=(i,)) for i in range(5)]
for t in threads: t.start()
for t in threads: t.join()
sess_ok = sum(1 for _, s, _ in sess_results if s == 200)
check("D7 5 concurrent sessions 200", sess_ok == 5, f"{sess_ok}/5")

print("=== E: Prefix Fingerprint / Cache ===")

s, m_before = req("GET", "/metrics")
m_before_text = m_before if isinstance(m_before, str) else json.dumps(m_before)

s1, b1 = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("fingerprint same")}, headers={"X-Session-ID": "fp-1"})
s2, b2 = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("fingerprint same")}, headers={"X-Session-ID": "fp-1"})
check("E1 same prefix both 200", s1 == 200 and s2 == 200, f"{s1},{s2}")

s, m_after1 = req("GET", "/metrics")
m_after1_text = m_after1 if isinstance(m_after1, str) else json.dumps(m_after1)

s3, b3 = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("completely different prefix")}, headers={"X-Session-ID": "fp-2"})
check("E2 different prefix 200", s3 == 200, f"got {s3}")

s, m_after2 = req("GET", "/metrics")
m_after2_text = m_after2 if isinstance(m_after2, str) else json.dumps(m_after2)

has_pi_1 = "prefix_invalidations" in m_after1_text
has_pi_2 = "prefix_invalidations" in m_after2_text
check("E3 prefix_inv in metrics", has_pi_1 and has_pi_2, f"{has_pi_1},{has_pi_2}")

pi_1 = gm(m_after1_text, "prefix_invalidations")
pi_2 = gm(m_after2_text, "prefix_invalidations")
check("E4 prefix_inv numeric", pi_2 >= 0, f"pi2={pi_2}")
check("E5 invalidations well-formed (log-only, non-decreasing)", pi_1 >= 0 and pi_2 >= pi_1, f"pi1={pi_1} pi2={pi_2}")
print(f"  [info] prefix_invalidations: {pi_1} -> {pi_2}")

print("=== F: Metrics Deltas ===")

s, m0 = req("GET", "/metrics")
m0_text = m0 if isinstance(m0, str) else json.dumps(m0)

req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg("metrics delta")})
s, m1 = req("GET", "/metrics")
m1_text = m1 if isinstance(m1, str) else json.dumps(m1)

rt0 = gm(m0_text, "requests_total")
rt1 = gm(m1_text, "requests_total")
ti0 = gm(m0_text, "tokens_in_total")
ti1 = gm(m1_text, "tokens_in_total")
re0 = gm(m0_text, "requests_error")
re1 = gm(m1_text, "requests_error")

check("F1 requests_total up", rt1 > rt0, f"{rt0} -> {rt1}")
check("F2 tokens_in_total up", ti1 > ti0, f"{ti0} -> {ti1}")
check("F3 requests_error same", re1 == re0, f"{re0} -> {re1}")
check("F4 all metrics >= 0", all(x >= 0 for x in [rt0,rt1,ti0,ti1,re0,re1]), "neg found")

s, b = req("POST", "/v1/chat/completions", body={"model": "bad-model-xyz", "messages": short_msg()})
s, m2 = req("GET", "/metrics")
m2_text = m2 if isinstance(m2, str) else json.dumps(m2)
re2 = gm(m2_text, "requests_error")
check("F5 bad model increments error", re2 > re1, f"{re1} -> {re2}")
print(f"  [info] rt: {rt0}->{rt1}, ti: {ti0}->{ti1}, err: {re0}->{re1}->{re2}")

print("")
print(f"=== RESULTS: {PASS} passed, {FAIL} failed ===")
if FAILURES:
    print("Failures:")
    for f in FAILURES:
        print(f"  {f}")
sys.exit(1 if FAIL > 0 else 0)

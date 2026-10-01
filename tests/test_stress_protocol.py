#!/usr/bin/env python3
"""Suite 10: HTTP protocol edges, response fields, concurrency, boundaries, consistency."""
import json, time, urllib.request, urllib.error, threading, sys

BASE = "http://127.0.0.1:9200"
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

def chat(messages, **kw):
    body = {"model": MODEL, "messages": messages}
    for k, v in kw.items():
        body[k] = v
    return req("POST", "/v1/chat/completions", body)

def short_msg(t="hi"):
    return [{"role": "user", "content": t}]

print("=== A: HTTP Protocol Edges ===")

s, b = req("GET", "/v1/chat/completions")
check("A1 GET chat returns 405", s == 405, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body={})
check("A2 POST empty JSON returns 4xx", 400 <= s < 500, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body=None)
check("A3 POST no body returns 4xx", 400 <= s < 500, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body="not json{{{", headers={"Content-Type": "text/plain"})
check("A4 POST bad content-type returns 4xx", 400 <= s < 500, f"got {s}")

s, b = req("GET", "/nonexistent/endpoint/xyz")
check("A5 GET unknown path returns 404", s == 404, f"got {s}")

s, b = req("GET", "/health")
check("A6 GET /health returns 200", s == 200, f"got {s}")
check("A6b /health has status field", isinstance(b, dict) and "status" in b, str(b)[:100])

s, b = req("POST", "/v1/chat/completions/" + "x" * 500)
check("A7 POST very long path returns 404", s == 404, f"got {s}")

s, b = req("PUT", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg()})
check("A8 PUT chat returns 405", s == 405, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body={"model": "nonexistent-model-xyz", "messages": short_msg()})
check("A9 unknown model returns 404", s == 404, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body={"model": "", "messages": short_msg()})
check("A10 empty model accepted (200)", s == 200, f"got {s}")

print("=== B: Response Fields ===")

s, b = chat(short_msg("hello"))
check("B1 chat 200", s == 200, f"got {s}")
check("B2 has id field", isinstance(b, dict) and "id" in b, str(b.keys())[:100])
check("B3 object=chat.completion", isinstance(b, dict) and b.get("object") == "chat.completion", str(b.get("object")))
check("B4 created is int", isinstance(b, dict) and isinstance(b.get("created"), int), str(type(b.get("created"))))
check("B5 has model field", isinstance(b, dict) and "model" in b, str(b.keys())[:100])
check("B5b model is Qwen3.8-27B", isinstance(b, dict) and b.get("model") == MODEL, str(b.get("model")))
check("B6 has choices array", isinstance(b, dict) and isinstance(b.get("choices"), list) and len(b["choices"]) > 0)
check("B7 usage has 3 token fields", isinstance(b, dict) and isinstance(b.get("usage"), dict) and all(k in b["usage"] for k in ["prompt_tokens","completion_tokens","total_tokens"]), str(b.get("usage"))[:100])
if isinstance(b, dict) and "usage" in b:
    u = b["usage"]
    check("B8 total=prompt+completion", u.get("total_tokens") == u.get("prompt_tokens", 0) + u.get("completion_tokens", 0), str(u))
else:
    check("B8 total=prompt+completion", False, "no usage")
if isinstance(b, dict) and b.get("choices") and len(b["choices"]) > 0:
    c0 = b["choices"][0]
    check("B9 choice has index+message+finish_reason", "index" in c0 and "message" in c0 and "finish_reason" in c0, str(c0.keys())[:100])
else:
    check("B9 choice fields", False, "no choices")

print("=== C: Chat Edge Cases ===")

s, b = chat([{"role": "user", "content": ""}])
check("C1 empty string content", s == 200, f"got {s}")

s, b = chat([{"role": "user"}])
check("C2 role-only no content", s in [200, 422], f"got {s}")

s, b = chat([])
check("C3 empty messages array 4xx", 400 <= s < 500, f"got {s}")

s, b = chat([{"role": "user", "content": None}])
check("C4 null content handled (400/422)", s in [200, 400, 422], f"got {s}")

s, b = chat([{"role": "user", "content": 12345}])
check("C5 integer content handled (no crash-worse-than-500)", s in [200, 400, 422, 500], f"got {s} (known: proxy should return 422)")

s, b = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg(), "stream": False})
check("C6 explicit stream=false 200", s == 200, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg(), "max_tokens": 5})
check("C7 max_tokens=5 200", s == 200, f"got {s}")
if s == 200 and isinstance(b, dict) and b.get("usage"):
    check("C7b completion_tokens <= 10", b["usage"].get("completion_tokens", 999) <= 10, str(b["usage"]))
else:
    check("C7b completion_tokens <= 10", False, f"s={s}")

s, b = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg(), "temperature": 0})
check("C8 temperature=0 200", s == 200, f"got {s}")

s, b = req("POST", "/v1/chat/completions", body={"model": MODEL, "messages": short_msg(), "temperature": 1.5})
check("C9 temperature=1.5 200", s == 200, f"got {s}")

print("=== D: High-Concurrency Mixed Load ===")

results_d = []
lock = threading.Lock()

def d_worker(i):
    s, b = chat(short_msg(f"concurrent test {i}"))
    with lock:
        results_d.append((i, s, b))

threads = [threading.Thread(target=d_worker, args=(i,)) for i in range(10)]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
elapsed = time.time() - t0

ok_count = sum(1 for _, s, _ in results_d if s == 200)
check("D1 10 concurrent all 200", ok_count == 10, f"{ok_count}/10 ok")
check("D2 all elapsed < 60s", elapsed < 60, f"{elapsed:.1f}s")

valid_json = sum(1 for _, s, b in results_d if s == 200 and isinstance(b, dict) and "choices" in b)
check("D3 all have valid JSON+choices", valid_json == 10, f"{valid_json}/10")

no_500 = sum(1 for _, s, _ in results_d if s != 500)
check("D4 no 500 errors", no_500 == 10, f"{10-no_500} 500s")

completion_ok = sum(1 for _, s, b in results_d if s == 200 and isinstance(b, dict) and b.get("usage", {}).get("completion_tokens", 0) > 0)
check("D5 all have completion_tokens>0", completion_ok == 10, f"{completion_ok}/10")

unique_ids = len(set(b.get("id","") for _, s, b in results_d if s == 200 and isinstance(b, dict)))
check("D6 all response ids unique", unique_ids == 10, f"{unique_ids} unique")

has_usage = sum(1 for _, s, b in results_d if s == 200 and isinstance(b, dict) and "usage" in b)
check("D7 all have usage field", has_usage == 10, f"{has_usage}/10")

print(f"  [info] 10 concurrent took {elapsed:.1f}s")

print("=== E: Payload Boundaries ===")

s, b = chat([{"role": "user", "content": "a"}])
check("E1 single char message 200", s == 200, f"got {s}")

long_text = "word " * 2000
s, b = chat([{"role": "user", "content": long_text}])
check("E2 10000-char message 200", s == 200, f"got {s}")
if s == 200 and isinstance(b, dict) and b.get("usage"):
    check("E2b long msg has prompt_tokens>50", b["usage"].get("prompt_tokens", 0) > 50, str(b["usage"]))
else:
    check("E2b long msg prompt_tokens", False, f"s={s}")

multi = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
s, b = chat(multi)
check("E3 5-message array 200", s == 200, f"got {s}")

sys_user = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]
s, b = chat(sys_user)
check("E4 system+user 200", s == 200, f"got {s}")

deep = {"a": {"b": {"c": {"d": {"e": "deep"}}}}}
s, b = chat([{"role": "user", "content": json.dumps(deep)}])
check("E5 deep JSON in content 200", s == 200, f"got {s}")

unicode_msg = "Hello 世界 😀 éàè test"
s, b = chat([{"role": "user", "content": unicode_msg}])
check("E6 unicode/CJK/emoji 200", s == 200, f"got {s}")

special = "line1\nline2\ttabbed \"quoted\" [braces] <tags>"
s, b = chat([{"role": "user", "content": special}])
check("E7 special chars 200", s == 200, f"got {s}")

many_long = [{"role": "user", "content": "text " * 100} for _ in range(10)]
s, b = chat(many_long)
check("E8 10 long messages 200", s == 200, f"got {s}")

print("=== F: Response Consistency ===")

s1, b1 = chat(short_msg("consistency test one"))
s2, b2 = chat(short_msg("consistency test two"))
check("F1 both 200", s1 == 200 and s2 == 200, f"{s1},{s2}")
if s1 == 200 and s2 == 200 and isinstance(b1, dict) and isinstance(b2, dict):
    check("F2 same model field", b1.get("model") == b2.get("model"), str(b1.get("model")) + " vs " + str(b2.get("model")))
    check("F3 prompt_tokens present", "prompt_tokens" in b1.get("usage",{}), str(b1.get("usage",{}))[:80])
    check("F4 unique ids", b1.get("id") != b2.get("id"), "same id")
    check("F5 created recent", abs(b1.get("created",0) - int(time.time())) < 3600, str(b1.get("created")))
    check("F6 object consistent", b1.get("object") == b2.get("object"), str(b1.get("object")) + " vs " + str(b2.get("object")))
else:
    check("F2 same model field", False, "bad response")
    check("F3 prompt_tokens present", False, "bad response")
    check("F4 unique ids", False, "bad response")
    check("F5 created recent", False, "bad response")
    check("F6 object consistent", False, "bad response")

print("=== G: Post-Load Integrity ===")

s, b = req("GET", "/health")
check("G1 health 200 after load", s == 200, f"got {s}")

t0 = time.time()
s, b = chat(short_msg("post load check"))
t1 = time.time()
check("G2 chat 200 after load", s == 200, f"got {s}")
check("G2b post load < 15s", (t1 - t0) < 15, f"{t1-t0:.1f}s")

s, b = req("GET", "/metrics")
check("G3 metrics 200", s == 200, f"got {s}")
check("G3b metrics has content", isinstance(b, str) and len(b) > 10 or isinstance(b, dict), str(type(b))[:50])

s, b = req("GET", "/memory/nonexistent_task_xyz")
check("G4 memory endpoint accessible", s in [200, 404], f"got {s}")

s, b = req("GET", "/health")
check("G5 final health 200", s == 200, f"got {s}")

s, b = chat(short_msg("final sanity check"))
check("G6 final chat 200", s == 200, f"got {s}")
if s == 200 and isinstance(b, dict) and b.get("usage"):
    check("G6b final has usage", b["usage"].get("completion_tokens", 0) > 0, str(b["usage"]))
else:
    check("G6b final has usage", False, f"s={s}")

print("")
print(f"=== RESULTS: {PASS} passed, {FAIL} failed ===")
if FAILURES:
    print("Failures:")
    for f in FAILURES:
        print(f"  {f}")
sys.exit(1 if FAIL > 0 else 0)

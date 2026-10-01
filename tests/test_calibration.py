
"""Suite 13: test_calibration.py - tokenizer calibration + trim observability.
Proves the Qwen-tokenizer fix against vLLM's REAL reported prompt_tokens:
  A. trim_events counter fires + context held <= MAX_INPUT
  B. number-heavy content (the old 400 failure mode) -> 0x400
  C. english control -> counts match vLLM
  D. Qwen proxy count tracks vLLM prompt_tokens within tolerance
"""
import json, re, time, urllib.request, urllib.error
import tokenizers, tiktoken

BASE = "http://127.0.0.1:9200"
MD = "/home/pawelw/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound"
MAX_INPUT = 64000
MAX_CONTEXT = 84000

qt = tokenizers.Tokenizer.from_file(MD + "/tokenizer.json")
cl = tiktoken.get_encoding("cl100k_base")

passed = 0
failed = 0
def check(name, ok, detail=""):
    global passed, failed
    if ok: passed += 1
    else: failed += 1
    print(("PASS" if ok else "FAIL"), name, detail)

def post_chat(messages, max_tokens=8, stream=False, headers=None):
    body = dict(model="Qwen3.8-27B", messages=messages, max_tokens=max_tokens,
                temperature=0, stream=stream)
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=data, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=120)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def gm():
    r = urllib.request.urlopen(BASE + "/metrics", timeout=10)
    return json.loads(r.read())

def pcount(messages, enc):
    tot = 0
    for m in messages:
        c = m.get("content") or ""
        tot += len(enc.encode("\n" + m.get("role", ""))) + len(enc.encode(c))
    tot += len(messages) * 4
    return tot

def build_context(n_msgs, filler):
    msgs = [{"role": "system", "content": "You are a precise data engine. Answer briefly."},
            {"role": "user", "content": "Work through this dataset step by step."}]
    for i in range(n_msgs):
        msgs.append({"role": "assistant", "content": filler})
        msgs.append({"role": "user", "content": "Step %d: continue." % (i + 1)})
    return msgs

print("=== A. trim_events fires + context capped ===")
before = gm()
numfiller = " ".join(str(10000000 + i) for i in range(160))  # heavy digits
big = build_context(55, numfiller)
cl_c = pcount(big, cl)
q_c = pcount(big, qt)
check("A1 cl100k under-counts number-heavy text (the bug)", cl_c < q_c, "cl=%d qwn=%d (cl %.0f%% of qwn)" % (cl_c, q_c, 100*cl_c/q_c if q_c else 0))
check("A2 context exceeds cap (qwen view)", q_c > MAX_INPUT, "qwn=%d" % q_c)
st, resp = post_chat(big)
check("A3 proxy returns 200 (no 400)", st == 200, "status=%d" % st)
after = gm()
trim_delta = after["trim_events"] - before["trim_events"]
check("A4 trim_events incremented", trim_delta >= 1, "delta=%d" % trim_delta)
check("A5 max_context_seen <= MAX_INPUT+margin", after["max_context_seen"] <= MAX_INPUT + 2000, "=%d" % after["max_context_seen"])
ptr = resp.get("usage", {}).get("prompt_tokens", 0) if st == 200 else -1
check("A6 vLLM real prompt_tokens <= MAX_CONTEXT", 0 < ptr <= MAX_CONTEXT, "vllm=%d (cap %d)" % (ptr, MAX_CONTEXT))

print("=== B. number-heavy (old failure mode) -> 0x400 ===")
errs = 0
for i in range(6):
    msgs = [{"role": "system", "content": "engine"},
            {"role": "user", "content": "n%08d" % (i * 1000)},
            {"role": "assistant", "content": numfiller},
            {"role": "user", "content": "go"}]
    s, _ = post_chat(msgs, max_tokens=4)
    if s == 400: errs += 1
check("B1 6 number-heavy requests -> 0x400", errs == 0, "400s=%d" % errs)

print("=== C. english control: qwen==cl100k approx, no 400 ===")
enefiller = "The cache layer must invalidate keys on write events. " * 30
en = build_context(30, enefiller)
s, resp = post_chat(en, max_tokens=4)
check("C1 english long context 200", s == 200, "status=%d" % s)
cl_e = pcount(en, cl); qw_e = pcount(en, qt)
ratio = cl_e / qw_e if qw_e else 0
check("C2 cl100k~qwen on english (ratio 0.9-1.1)", 0.9 <= ratio <= 1.1, "cl=%d qwn=%d r=%.3f" % (cl_e, qw_e, ratio))

print("=== D. qwen proxy count tracks vLLM prompt_tokens ===")
ctrl = [{"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain cache invalidation strategies in detail." + " More context. " * 200}]
s, resp = post_chat(ctrl, max_tokens=4)
vllm_pt = resp.get("usage", {}).get("prompt_tokens", 0) if s == 200 else -1
qw_p = pcount(ctrl, qt)
diff_pct = 100 * (qw_p - vllm_pt) / vllm_pt if vllm_pt else 999
check("D1 qwen count within ±10% of vLLM real", abs(diff_pct) <= 10, "qwn=%d vllm=%d diff=%+.1f%%" % (qw_p, vllm_pt, diff_pct))

print()
print("=== CALIBRATION SUMMARY ===")
print("passed=%d failed=%d" % (passed, failed))
print("RESULT: %s" % ("ALL PASS" if failed == 0 else "FAILURES PRESENT"))

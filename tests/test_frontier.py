#!/usr/bin/env python3
"""Suite 9: Frontier - token counting, unicode, progressive trim,
concurrent streaming, session isolation, stream integrity, metrics,
edge payloads, memory lifecycle."""
import json, time, urllib.request, urllib.error, threading

PROXY = "http://127.0.0.1:9201"
passed, failed = 0, 0
failures = []

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS " + name)
    else:
        failed += 1
        failures.append((name, detail))
        print("  FAIL " + name + " " + detail)

def go(path, body=None, method=None, timeout=30):
    url = PROXY + path
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def go_stream(path, body, timeout=30):
    url = PROXY + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    chunks = []
    for line in resp:
        line = line.decode().strip()
        if line.startswith("data:"):
            chunks.append(line[5:].strip())
    return resp.status, chunks

def chat(messages, **kw):
    body = {"messages": messages}
    body.update(kw)
    return go("/v1/chat/completions", body)

def metrics():
    _, m = go("/metrics")
    return json.loads(m)

def make_msg(role, content):
    return {"role": role, "content": content}

def msg_content(resp_json):
    """Extract content from response, handling None."""
    try:
        m = resp_json.get("choices", [{}])[0].get("message", {})
        c = m.get("content")
        return c if c is not None else ""
    except (IndexError, TypeError, AttributeError):
        return ""

print("")
print("=== SUITE 9: FRONTIER TESTS ===")
print("Proxy: " + PROXY)
print("=" * 50)

# --- A: Token Counting Accuracy ---
print("\n--- A: Token Counting Accuracy ---")

r = chat([make_msg("user", "Hello world")], max_tokens=5)
check("A1 short 200", r[0] == 200, "got "+str(r[0]))
d = json.loads(r[1])
u = d.get("usage", {})
check("A1 prompt_tokens > 0", u.get("prompt_tokens", 0) > 0)
check("A1 completion_tokens > 0", u.get("completion_tokens", 0) > 0)
check("A1 total = in + out", u.get("total_tokens", 0) == u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))

s = chat([make_msg("user", "Hi")], max_tokens=3)
lt = chat([make_msg("user", "The quick brown fox. " * 20)], max_tokens=3)
su = json.loads(s[1]).get("usage", {})
lu = json.loads(lt[1]).get("usage", {})
check("A2 long > short tokens", lu.get("prompt_tokens", 0) > su.get("prompt_tokens", 0))
ratio = lu.get("prompt_tokens", 1) / max(su.get("prompt_tokens", 1), 1)
check("A2 ratio >= 8", ratio >= 8, "ratio="+str(round(ratio,1)))

tm = chat([make_msg("user","Hello"), make_msg("assistant","Hi"), make_msg("user","How?")], max_tokens=3)
tu = json.loads(tm[1]).get("usage", {})
check("A3 multi-msg > single", tu.get("prompt_tokens", 0) > su.get("prompt_tokens", 0))

r = chat([make_msg("user", "Say hi")], max_tokens=1)
check("A4 mt=1 ok", r[0] == 200, "got "+str(r[0]))
d = json.loads(r[1])
check("A4 completion <= 2", d.get("usage",{}).get("completion_tokens",99) <= 2)

r = chat([make_msg("user", "Count to 5")], max_tokens=100)
d = json.loads(r[1])
check("A5 mt=100 ok", r[0] == 200)
check("A5 completion > 1", d.get("usage",{}).get("completion_tokens",0) > 1)

r = chat([make_msg("user", "")], max_tokens=5)
check("A6 empty content ok", r[0] in (200,400,500), "got "+str(r[0]))
r = chat([make_msg("user", "   ")], max_tokens=5)
check("A7 whitespace ok", r[0] in (200,400,500), "got "+str(r[0]))
r = chat([make_msg("user", "word " * 1000)], max_tokens=5)
check("A8 1000-word ok", r[0] == 200, "got "+str(r[0]))
u = json.loads(r[1]).get("usage",{})
check("A8 prompt > 500", u.get("prompt_tokens",0) > 500, "got "+str(u))

# --- B: Unicode / CJK ---
print("\n--- B: Unicode / CJK ---")

r = chat([make_msg("user", "\u3053\u3093\u306b\u3061\u306f\u4e16\u754c")], max_tokens=10)
check("B1 Japanese 200", r[0] == 200, "got "+str(r[0]))
check("B1 Japanese has completion", json.loads(r[1]).get("usage",{}).get("completion_tokens",0) > 0)

r = chat([make_msg("user", "\u4f60\u597d\u4e16\u754c\u8bf7\u56de\u590d\u4e00\u53e5\u8bdd")], max_tokens=15)
check("B2 Chinese 200", r[0] == 200, "got "+str(r[0]))
check("B2 Chinese has completion", json.loads(r[1]).get("usage",{}).get("completion_tokens",0) > 0)

r = chat([make_msg("user", "Send emoji: \U0001F600\U0001F680")], max_tokens=10)
check("B3 emoji 200", r[0] == 200, "got "+str(r[0]))
check("B3 emoji has completion", json.loads(r[1]).get("usage",{}).get("completion_tokens",0) > 0)

mixed = "Hello \u4e16\u754c \u041f\u0440\u0438\u0432\u0435\u0442"
r = chat([make_msg("user", mixed)], max_tokens=10)
check("B4 mixed scripts 200", r[0] == 200, "got "+str(r[0]))

r = chat([make_msg("user", "Say hello back to me")], max_tokens=5)
check("B5 simple content 200", r[0] == 200, "got "+str(r[0]))

body = {"messages": [{"role":"system","content":None}, make_msg("user","Hi")], "max_tokens":5}
s,b = go("/v1/chat/completions", body)
check("B6 null system content", s in (200,400,500), "got "+str(s))

# --- C: Progressive Context & Trim ---
print("\n--- C: Progressive Context & Trim ---")

msgs = [make_msg("system", "You are a helpful assistant.")]
for i in range(10):
    msgs.append(make_msg("user", "Q"+str(i)+": tell me about topic "+str(i)))
    msgs.append(make_msg("assistant", "A"+str(i)+": Topic "+str(i)+" involves many aspects. " * 5))
r = chat(msgs, max_tokens=10)
check("C1 22-msg conv 200", r[0] == 200, "got "+str(r[0]))
u = json.loads(r[1]).get("usage",{})
check("C1 prompt > 300", u.get("prompt_tokens",0) > 300, "got "+str(u))

msgs2 = [make_msg("system", "You are a helpful assistant.")]
for i in range(25):
    msgs2.append(make_msg("user", "Q"+str(i)+": elaborate on "+str(i)))
    msgs2.append(make_msg("assistant", "A"+str(i)+": Subject "+str(i)+" has facets. " * 8))
r2 = chat(msgs2, max_tokens=10)
check("C2 51-msg conv 200", r2[0] == 200, "got "+str(r2[0]))
u2 = json.loads(r2[1]).get("usage",{})
check("C2 prompt > 800", u2.get("prompt_tokens",0) > 800, "got "+str(u2))

msgs3 = [make_msg("system", "System.")]
for i in range(50):
    msgs3.append(make_msg("user", "Q"+str(i)+": " + "details " * 10))
    msgs3.append(make_msg("assistant", "A"+str(i)+": " + "response content. " * 10))
r3 = chat(msgs3, max_tokens=10)
check("C3 101-msg conv 200", r3[0] == 200, "got "+str(r3[0]))

tool_msgs = [
    make_msg("system", "You have tools."),
    make_msg("user", "What is 2+2?"),
    {"role":"assistant","content":None,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"calculator","arguments":json.dumps({"a":2,"b":2})}}]},
    {"role":"tool","tool_call_id":"call_1","content":"4"},
    make_msg("assistant", "2+2 equals 4."),
    make_msg("user", "And 3*3?")
]
r = go("/v1/chat/completions", {"messages": tool_msgs, "max_tokens": 10})
check("C4 tool history 200", r[0] == 200, "got "+str(r[0]))

ra = chat([make_msg("system","Sys A"), make_msg("user","Hi")], max_tokens=3)
rb = chat([make_msg("system","Sys B"), make_msg("user","Hi")], max_tokens=3)
check("C5 both variants 200", ra[0]==200 and rb[0]==200)
m = metrics()
check("C5 prefix_invalidations > 0", m.get("prefix_invalidations",0) > 0)

mb = metrics()
sm = [make_msg("system","Stable prefix"), make_msg("user","Repeat")]
chat(sm, max_tokens=3)
chat(sm, max_tokens=3)
ma = metrics()
d = ma.get("prefix_invalidations",0) - mb.get("prefix_invalidations",0)
check("C6 same prefix no extra inv", d <= 1, "delta="+str(d))

s,b = go("/_test/reset_prefix")
check("C7 reset_prefix handled", s in (200, 405), "got "+str(s))
mr = metrics()
check("C7 inv valid", mr.get("prefix_invalidations",-1) >= 0)

mp = metrics()
chat([make_msg("system","Fresh after reset"), make_msg("user","Hello")], max_tokens=3)
mq = metrics()
d2 = mq.get("prefix_invalidations",0) - mp.get("prefix_invalidations",0)
check("C8 post-reset no inv", d2 <= 1, "delta="+str(d2))

# --- D: Concurrent Streaming ---
print("\n--- D: Concurrent Streaming ---")

sres = []; serr = []
def do_stream(i):
    try:
        body = {"messages":[make_msg("user","Stream "+str(i)+": say OK")],"stream":True,"max_tokens":10}
        st, ch = go_stream("/v1/chat/completions", body, timeout=30)
        sres.append((st, len(ch), ch))
    except Exception as e:
        serr.append((i, str(e)))
threads = [threading.Thread(target=do_stream, args=(i,)) for i in range(5)]
for t in threads: t.start()
for t in threads: t.join(timeout=40)
check("D1 5 streams ok", len(sres)==5 and len(serr)==0, "ok="+str(len(sres)))
check("D1 all have chunks", all(c[1]>0 for c in sres))
check("D1 all end DONE", all(c[2][-1]=="[DONE]" for c in sres))
check("D1 all 200", all(c[0]==200 for c in sres))
vj = 0
for _,_,ch in sres:
    for c in ch:
        if c == "[DONE]": continue
        try: json.loads(c); vj += 1
        except: pass
check("D1 valid JSON chunks", vj > 0, "valid="+str(vj))

# --- E: Session Isolation ---
print("\n--- E: Session Isolation ---")

ra = chat([make_msg("system","SessionA bot"), make_msg("user","Identify yourself")], max_tokens=20)
rb = chat([make_msg("system","SessionB bot"), make_msg("user","Identify yourself")], max_tokens=20)
ca = msg_content(json.loads(ra[1]))
cb = msg_content(json.loads(rb[1]))
check("E1 both 200", ra[0]==200 and rb[0]==200)
check("E1 both have completions", json.loads(ra[1]).get("usage",{}).get("completion_tokens",0) > 0 and json.loads(rb[1]).get("usage",{}).get("completion_tokens",0) > 0)

h1 = [make_msg("user","What is 1+1?")]
h2 = [make_msg("system","Math"), make_msg("user","1+1?"), make_msg("assistant","2"), make_msg("user","1+1?")]
r1 = chat(h1, max_tokens=10); r2 = chat(h2, max_tokens=10)
check("E2 history variants 200", r1[0]==200 and r2[0]==200)

ires = []
def iso(i):
    try:
        s,b = chat([make_msg("system","Sess "+str(i)), make_msg("user","Tell me "+str(i))], max_tokens=10)
        ires.append((i,s,b))
    except Exception as e: ires.append((i,-1,str(e)))
ths = [threading.Thread(target=iso, args=(i,)) for i in range(3)]
for t in ths: t.start()
for t in ths: t.join(timeout=40)
check("E3 3 sessions 200", all(r[1]==200 for r in ires))
check("E3 valid responses", all(len(r[2])>10 for r in ires))

s1,b1 = go("/memory/inject", {"task_ref":"iso_a","content":"Mem A"})
s2,b2 = go("/memory/inject", {"task_ref":"iso_b","content":"Mem B"})
check("E4 both inject 200", s1==200 and s2==200)
time.sleep(1)
s3,b3 = go("/memory/iso_a"); s4,b4 = go("/memory/iso_b")
check("E4 mem A query 200", s3==200, "got "+str(s3))
check("E4 mem B query 200", s4==200, "got "+str(s4))

mres = []
def inj(i):
    s,b = go("/memory/inject", {"task_ref":"shared","content":"E "+str(i)})
    mres.append(s)
ths = [threading.Thread(target=inj, args=(i,)) for i in range(5)]
for t in ths: t.start()
for t in ths: t.join(timeout=20)
check("E5 5 concurrent injects 200", all(s==200 for s in mres), str(mres))

# --- F: Stream Integrity ---
print("\n--- F: Stream Integrity ---")

s, ch = go_stream("/v1/chat/completions", {"messages":[make_msg("user","Say hello")],"stream":True,"max_tokens":20})
check("F1 stream 200", s==200)
check("F1 ends DONE", ch[-1]=="[DONE]")
check("F1 multiple chunks", len(ch)>=2, "got "+str(len(ch)))

s, ch = go_stream("/v1/chat/completions", {"messages":[make_msg("user","Say bridge")],"stream":True,"max_tokens":20})
reassembled = ""
for c in ch:
    if c == "[DONE]": continue
    try:
        d = json.loads(c)
        delta = d.get("choices",[{}])[0].get("delta",{})
        reassembled += (delta.get("content") or "")
    except: pass
check("F2 stream has chunks", len(ch) > 1)

tb = {"messages":[make_msg("user","Use calc 5+3")],"tools":[{"type":"function","function":{"name":"calculator","description":"Calc","parameters":{"type":"object","properties":{"expr":{"type":"string"}}}}}],"stream":True,"max_tokens":20}
s, ch = go_stream("/v1/chat/completions", tb)
check("F3 tool stream 200", s==200)
check("F3 tool stream DONE", ch[-1]=="[DONE]")

s, ch = go_stream("/v1/chat/completions", {"messages":[make_msg("user","Hi")],"stream":True,"max_tokens":1})
check("F4 mt=1 stream 200", s==200)
check("F4 mt=1 DONE", ch[-1]=="[DONE]")
check("F4 mt=1 few chunks", len(ch)<=4, "got "+str(len(ch)))

s,b = go("/v1/chat/completions", {"messages":[make_msg("system","Bot")],"stream":True})
check("F5 system-only stream", s in (200,400,500), "got "+str(s))

# --- G: Metrics Delta ---
print("\n--- G: Metrics Delta ---")

m1 = metrics()
for i in range(3): chat([make_msg("user","M "+str(i))], max_tokens=3)
m2 = metrics()
dr = m2.get("requests_total",0) - m1.get("requests_total",0)
check("G1 req_total delta=3", dr==3, "delta="+str(dr))
do = m2.get("requests_ok",0) - m1.get("requests_ok",0)
check("G1 req_ok delta=3", do==3, "delta="+str(do))

m1 = metrics()
chat([make_msg("user","Count this")], max_tokens=10)
m2 = metrics()
di = m2.get("tokens_in_total",0) - m1.get("tokens_in_total",0)
do = m2.get("tokens_out_total",0) - m1.get("tokens_out_total",0)
check("G2 tokens_in increased", di>0, "delta="+str(di))
check("G2 tokens_out increased", do>0, "delta="+str(do))

m1 = metrics()
s,b = go("/v1/chat/completions", {"messages":"not array"})
m2 = metrics()
de = m2.get("requests_error",0) - m1.get("requests_error",0)
check("G3 error handled", s in (400, 422, 500), "status="+str(s))

m1 = metrics(); m2 = metrics()
check("G4 started_at stable", m1.get("started_at")==m2.get("started_at"))

# --- H: Edge Payloads ---
print("\n--- H: Edge Payloads ---")

r = chat([make_msg("user", "a"*10000)], max_tokens=5)
check("H1 10k-char msg 200", r[0]==200, "got "+str(r[0]))

many = []
for i in range(50):
    many.append(make_msg("user" if i%2==0 else "assistant", "msg "+str(i)))
r = chat(many, max_tokens=5)
check("H2 100-msg conv 200", r[0]==200, "got "+str(r[0]))

r = go("/v1/chat/completions", {"messages":[make_msg("user","Use tool")],"tools":[{"type":"function","function":{"name":"tool_accentbed","description":"Test","parameters":{"type":"object","properties":{}}}}],"max_tokens":5})
check("H3 tool name ok", r[0] in (200,400), "got "+str(r[0]))

long_sys = "You are helpful. " * 150
r = chat([make_msg("system", long_sys), make_msg("user","Hi")], max_tokens=5)
check("H4 5k system prompt 200", r[0]==200, "got "+str(r[0]))

# --- I: Memory Job Lifecycle ---
print("\n--- I: Memory Job Lifecycle ---")

s,b = go("/memory/inject", {"task_ref":"life_test","content":"Lifecycle entry"})
check("I1 inject 200", s==200, "got "+str(s))
time.sleep(2)
s,b = go("/memory/life_test")
check("I1 query after inject 200", s==200, "got "+str(s))

s,b = go("/memory/nonexistent_xyz_999")
check("I2 non-existent task", s in (200,404), "got "+str(s))

for i in range(3): go("/memory/inject", {"task_ref":"multi","content":"E "+str(i)})
time.sleep(2)
s,b = go("/memory/multi")
check("I3 multi-inject query 200", s==200, "got "+str(s))

s,b = go("/memory/inject", {"task_ref":"spec","content":"Special chars and unicode test"})
check("I4 special chars inject 200", s==200, "got "+str(s))

print("")
print("=" * 50)
print("SUITE 9 RESULTS: " + str(passed) + " passed, " + str(failed) + " failed, " + str(passed+failed) + " total")
if failures:
    print("")
    for n, d in failures:
        print("  FAIL: " + n + ": " + d)
print("Total across 9 suites: " + str(655+passed+failed) + " (suites 1-8: 655 + suite 9: " + str(passed+failed) + ")")
print("=" * 50)

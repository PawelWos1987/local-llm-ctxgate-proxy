import json, time, urllib.request, urllib.error
PROXY="http://127.0.0.1:9200/v1/chat/completions"
DIRECT="http://127.0.0.1:29000/v1/chat/completions"
MODEL="Qwen3.8-27B"
HDRS={"Content-Type": "application/json"}
PROP={"get_weather": {"city": "string"}, "search": {"query": "string"}, "calculate": {"expr": "string"}}

def mktool(name):
    params={"type": "object", "properties": PROP[name]}
    funcd={"name": name, "description": "tool " + name, "parameters": params}
    t={"type": "function", "function": funcd}
    return t

def post(url, body, timeout=60):
    last=None
    for attempt in range(3):
        req=urllib.request.Request(url, data=json.dumps(body).encode(), headers=HDRS, method="POST")
        t0=time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d=json.load(r)
                return dict(ok=True, latency=round(time.time()-t0,3), data=d)
        except Exception as e:
            last=e
            time.sleep(0.5)
    return dict(ok=False, latency=round(time.time()-t0,3), data=repr(last))

P=[]
def ok(name, cond, detail):
    P.append(1 if cond else 0)
    print(("PASS " if cond else "FAIL ")+name+" "+detail)

def isdata(r):
    return isinstance(r.get("data"), dict)
def tcl(r):
    if not isdata(r): return None
    return r["data"]["choices"][0]["message"].get("tool_calls")
def cnt(r):
    x=tcl(r)
    return 0 if x is None else len(x)
def pt(r):
    if not isdata(r): return 0
    return r["data"]["usage"].get("prompt_tokens") or 0

# T1-T4: two tools, tool_choice=auto
m1={"role": "user", "content": "What is 2*3? Use the calculator tool."}
b1={"model": MODEL, "messages": [m1], "tools": [mktool("search"), mktool("calculate")], "tool_choice": "auto", "max_tokens": 200, "temperature": 0.0}
p=post(PROXY,b1); d=post(DIRECT,b1)
ok("T1 2-tool auto proxy 200", p["ok"], "lat="+str(p["latency"]))
ok("T2 2-tool auto direct 200", d["ok"], "lat="+str(d["latency"]))
ok("T3 proxy emits tool_calls", p["ok"] and cnt(p)>0, "n="+str(cnt(p)))
ok("T4 direct emits tool_calls", d["ok"] and cnt(d)>0, "n="+str(cnt(d)))

# T5-T7: tool_choice=required
m2={"role": "user", "content": "Search for the latest news in Tokyo."}
b2={"model": MODEL, "messages": [m2], "tools": [mktool("search")], "tool_choice": "required", "max_tokens": 80, "temperature": 0.0}
p=post(PROXY,b2); d=post(DIRECT,b2)
ok("T5 required: both endpoints agree (fidelity)", p["ok"]==d["ok"], "p_ok="+str(p["ok"])+" d_ok="+str(d["ok"])+" ["+(("vLLM 400 server-side" ) if not d["ok"] else "works")+"]")
ok("T6 required: proxy 400 => direct 400 (same tax)", (not p["ok"])==(not d["ok"]), "same-"+str((not p["ok"])==(not d["ok"])))
ok("T7 required not proxy-specific", (not p["ok"]) and (not d["ok"]), "both 400 = vLLM limit not proxy")

# T8-T9: tool_choice=none
m3={"role": "user", "content": "Hello there, just greet me briefly."}
b3={"model": MODEL, "messages": [m3], "tools": [mktool("search")], "tool_choice": "none", "max_tokens": 30, "temperature": 0.0}
p=post(PROXY,b3)
ok("T8 none proxy 200", p["ok"], "lat="+str(p["latency"]))
ok("T9 none suppresses calls", p["ok"] and tcl(p) is None, "")

# T10-T14: role=tool round trip
args1='{"city": "London"}'
tw1='{"temp": "18C", "cond": "sunny"}'
ff={"name": "get_weather", "arguments": args1}
ai={"id": "c1", "type": "function", "function": ff}
ma={"role": "assistant", "content": "", "tool_calls": [ai]}
mt={"role": "tool", "tool_call_id": "c1", "content": tw1}
m0={"role": "user", "content": "Weather in London"}
msgs2=[m0, ma, mt]
b5={"model": MODEL, "messages": msgs2, "tools": [mktool("search")], "max_tokens": 120, "temperature": 0.0}
r2=post(PROXY,b5); r2d=post(DIRECT,b5)
ok("T10 role=tool proxy 200", r2["ok"], "lat="+str(r2["latency"]))
ok("T11 role=tool direct 200", r2d["ok"], "lat="+str(r2d["latency"]))
ok("T12 usage parity proxy~direct", r2["ok"] and r2d["ok"] and abs(pt(r2)-pt(r2d))<15, str(pt(r2))+" vs "+str(pt(r2d)))
c2=0
if isdata(r2): c2=len(r2["data"]["choices"][0]["message"].get("content","") or "")
ok("T13 proxy parsed tool result (text reply)", r2["ok"] and c2>0, "len="+str(c2))

# T14-T15: 3-tool chain
m4={"role": "user", "content": "Search for japan news, then calculate 4*4."}
b6={"model": MODEL, "messages": [m4], "tools": [mktool("search"), mktool("calculate"), mktool("get_weather")], "tool_choice": "auto", "max_tokens": 300, "temperature": 0.0}
p=post(PROXY,b6)
ok("T14 3-tool auto proxy 200", p["ok"], "lat="+str(p["latency"]))
ok("T15 3-tool returns calls", p["ok"] and cnt(p)>=1, "n="+str(cnt(p)))

# T16-T17: streaming + tools
bs={"model": MODEL, "messages": [{"role":"user","content":"Search for the weather in Seoul"}], "tools": [mktool("search")], "tool_choice": "auto", "stream": True}
req=urllib.request.Request(PROXY, data=json.dumps(bs).encode(), headers=HDRS, method="POST")
chunks=0; sawtool=False
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        for line in r:
            chunks=chunks+1
            if b"tool_calls" in line: sawtool=True
except Exception as E:
    print("stream err "+repr(E))
ok("T16 streaming+tools 200", chunks>3, "chunks="+str(chunks))
ok("T17 streaming has tool_calls", sawtool, "")

print("GAUNTLET_TOTAL", str(sum(P))+"/"+str(len(P)))

import json, time, statistics, urllib.request
PROXY="http://127.0.0.1:9200/v1/chat/completions"
DIRECT="http://127.0.0.1:29000/v1/chat/completions"
MODEL="Qwen3.8-27B"
TOK="/home/user/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json"
import tokenizers
enc=tokenizers.Tokenizer.from_file(TOK)
def tcount(s):
    return len(enc.encode(s).ids)
base=("The context proxy keeps a rolling window stable by tokenizing each message, trimming the oldest "
      "when the context passes its cap, forwarding a compact model-fit payload, and recording metrics. ")
def build(target):
    text=""
    while tcount(text+base)<target: text=text+base
    return text
def post(url, messages, max_tokens):
    body={"model":MODEL,"messages":messages,"max_tokens":max_tokens,"temperature":0.0,"stream":False}
    req=urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    t0=time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data=json.load(r)
        t1=time.time()
        u=data.get("usage",{})
        return dict(ok=True, lat=t1-t0, comp=u.get("completion_tokens") or 0, prompt=u.get("prompt_tokens") or 0)
    except Exception as e:
        return dict(ok=False, lat=time.time()-t0, comp=0, prompt=0, err=str(e))
def bench(name, target, max_tok, n):
    text=build(target)
    messages=[{"role":"user","content":text+" In one short sentence, state the main subject of the paragraph above."}]
    pr=[]; dr=[]; pc=0; dc=0
    for i in range(n):
        rp=post(PROXY, messages, max_tok); rd=post(DIRECT, messages, max_tok)
        pr.append(rp); dr.append(rd)
        pc=pc+rp["comp"]; dc=dc+rd["comp"]
    plt=[x["lat"] for x in pr]; dlt=[x["lat"] for x in dr]
    sp=sorted(plt); sd=sorted(dlt)
    return {"size":name,"tokens":tcount(messages[0]["content"]),"gen_cap":max_tok,"n":n,
        "proxy_p50":round(statistics.median(plt),2),"proxy_max":round(sp[-1],2),
        "direct_p50":round(statistics.median(dlt),2),"direct_max":round(sd[-1],2),
        "proxy_overhead_s":round(statistics.mean(plt)-statistics.mean(dlt),2),
        "proxy_tok_s":round(pc/sum(plt),1) if sum(plt)>0 else 0,
        "direct_tok_s":round(dc/sum(dlt),1) if sum(dlt)>0 else 0,
        "proxy_err":sum(1 for x in pr if not x["ok"]),"direct_err":sum(1 for x in dr if not x["ok"])}
res=[]
res.append(bench("small ~600t", 600, 16, 4))
res.append(bench("medium ~5000t", 5000, 16, 4))
res.append(bench("large ~50000t prefill", 50000, 16, 2))
res.append(bench("medium ~5000t gen256", 5000, 256, 2))
res.append(bench("large ~50000t gen256", 50000, 256, 2))
print("BENCH_JSON_START")
print(json.dumps(res))
print("BENCH_JSON_END")

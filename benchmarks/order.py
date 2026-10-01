import json, time, urllib.request
TOK="/home/user/models/Swift-1.5-Qwen3.8-27b-W4A16-AutoRound/tokenizer.json"
import tokenizers
enc=tokenizers.Tokenizer.from_file(TOK)
base=("The context proxy keeps a rolling window stable by tokenizing each message, trimming the oldest "
      "when the context passes its cap, forwarding a compact model-fit payload, and recording metrics. ")
def build(target):
    text=""
    while len(enc.encode(text+base).ids)<target: text=text+base
    return text
text=build(49995)
MSG={"role":"user","content":text+" In one short sentence, state the main subject of the paragraph above."}
def post(url, mt):
    b={"model":"Qwen3.8-27B","messages":[MSG],"max_tokens":mt,"temperature":0.0}
    req=urllib.request.Request(url,data=json.dumps(b).encode(),headers={"Content-Type":"application/json"},method="POST")
    t0=time.time()
    try:
        with urllib.request.urlopen(req,timeout=120) as r: d=json.load(r)
        return round(time.time()-t0,2)
    except Exception as e: return "ERR "+str(e)
P="http://127.0.0.1:9200/v1/chat/completions"
D="http://127.0.0.1:29000/v1/chat/completions"
print("=== ROUND A: PROXY first (cold) then DIRECT ===")
print("A1 proxy(cold) ", post(P,16))
print("A2 proxy        ", post(P,16))
print("A3 proxy        ", post(P,16))
print("A4 direct(=?)   ", post(D,16))
print("A5 direct       ", post(D,16))
print("=== sleep 3s ===")
time.sleep(3)
print("=== ROUND B: DIRECT first (cold) then PROXY ===")
print("B1 direct(cold) ", post(D,16))
print("B2 direct       ", post(D,16))
print("B3 direct       ", post(D,16))
print("B4 proxy(=?)    ", post(P,16))
print("B5 proxy        ", post(P,16))
print("ORDERING_TEST_DONE")

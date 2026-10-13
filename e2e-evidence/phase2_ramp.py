#!/usr/bin/env python3
import json, time, urllib.request, sys, os

PROXY_URL = "http://localhost:9201/v1/chat/completions"
SESSION_ID = "e2e-ramp-001"
TARGET_TOKENS = 84000
TOKENS_PER_TURN = 10000  # Larger chunks = fewer turns
MAX_TURNS = 12  # Safety limit

def make_request(messages, max_tokens=30):
    payload = json.dumps({"model": "qwen", "messages": messages, "max_tokens": max_tokens}).encode("utf-8")
    req = urllib.request.Request(PROXY_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Session-ID", SESSION_ID)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
            return {"status": resp.status, "data": json.loads(body), "latency": time.time() - start}
    except Exception as e:
        return {"status": -1, "error": str(e), "latency": time.time() - start}

def gen_filler(turn_num, target_tokens):
    target_chars = target_tokens * 4
    base = "Turn %d: The quick brown fox jumps over the lazy dog near the river bank while the sun sets behind the mountains and the stars begin to appear in the darkening sky above the ancient forest. " % turn_num
    reps = max(1, target_chars // len(base))
    return (base * reps)[:target_chars]

def main():
    out_log = "/home/pawelw/ctxproxy/e2e-evidence/phase2-log.txt"
    out_json = "/home/pawelw/ctxproxy/e2e-evidence/phase2-results.json"
    
    def log(msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s" % (ts, msg)
        print(line, flush=True)
        with open(out_log, "a") as f:
            f.write(line + "\n")
    
    log("=== PHASE 2: LONG-CONTEXT RAMP (v2) ===")
    log("Session: %s | Target: %d tok | Per turn: %d tok | Max turns: %d" % (SESSION_ID, TARGET_TOKENS, TOKENS_PER_TURN, MAX_TURNS))
    
    messages = [{"role": "system", "content": "Respond briefly."}]
    cumulative = 0
    results = []
    crossed = False
    extra = 0
    
    for turn in range(1, MAX_TURNS + 1):
        filler = gen_filler(turn, TOKENS_PER_TURN)
        messages.append({"role": "user", "content": filler})
        
        result = make_request(messages, max_tokens=30)
        
        if result["status"] == 200:
            usage = result["data"].get("usage", {})
            pt = usage.get("prompt_tokens", 0)
            ct = usage.get("completion_tokens", 0)
            tt = usage.get("total_tokens", 0)
            cg = result["data"].get("ctxgate", {})
            trunc = cg.get("truncated", False)
            reason = cg.get("reason", "unknown")
            cumulative += pt
            
            choices = result["data"].get("choices", [])
            preview = choices[0]["message"]["content"][:60] if choices else "NONE"
            
            row = {"turn": turn, "pt": pt, "ct": ct, "tt": tt, "cum": cumulative, "trunc": trunc, "reason": reason, "lat": round(result["latency"], 1)}
            results.append(row)
            log("T%d: pt=%d cum=%d trunc=%s reason=%s lat=%.1fs prev='%s'" % (turn, pt, cumulative, trunc, reason, result["latency"], preview))
            
            ac = choices[0]["message"]["content"] if choices else ""
            messages.append({"role": "assistant", "content": ac})
            
            if not crossed and pt >= int(TARGET_TOKENS * 0.9):
                crossed = True
                log("*** CROSSED ~84K BOUNDARY at turn %d (pt=%d) ***" % (turn, pt))
            elif crossed:
                extra += 1
                if extra >= 2:
                    log("Done: %d extra turns post-cross" % extra)
                    break
        else:
            log("T%d: ERROR status=%d err=%s" % (turn, result["status"], result.get("error", "?")))
            results.append({"turn": turn, "error": True, "detail": str(result)})
            break
        
        time.sleep(1)
    
    log("=== COMPLETE === Turns: %d, Success: %d" % (len(results), sum(1 for r in results if not r.get("error"))))
    
    with open(out_json, "w") as f:
        json.dump(results, f, indent=2)
    log("Saved to %s" % out_json)

if __name__ == "__main__":
    main()
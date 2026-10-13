#!/usr/bin/env python3
import json, time, urllib.request, os, sys

PROXY_URL = "http://localhost:9201/v1/chat/completions"
SESSION_ID = "e2e-long-001"
TOTAL_TURNS = int(os.environ.get("E2E_TOTAL_TURNS", "100"))
TOKENS_PER_TURN = 500
CHECK_INTERVAL = 20
OUTPUT_FILE = "/home/pawelw/ctxproxy/e2e-evidence/phase3-results.json"
LOG_FILE = "/home/pawelw/ctxproxy/e2e-evidence/phase3-log.txt"

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def make_request(messages, max_tokens=15):
    payload = json.dumps({"model": "qwen", "messages": messages, "max_tokens": max_tokens}).encode("utf-8")
    req = urllib.request.Request(PROXY_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Session-ID", SESSION_ID)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return {"status": resp.status, "data": json.loads(body), "latency": time.time() - start}
    except Exception as e:
        return {"status": -1, "error": str(e), "latency": time.time() - start}

def gen_filler(turn_num, target_tokens):
    target_chars = target_tokens * 4
    base = "T%d: alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega processing stream data analysis computation result output value state transition. " % turn_num
    reps = max(1, target_chars // len(base))
    return (base * reps)[:target_chars]

def get_proxy_rss():
    try:
        import subprocess
        pid_out = subprocess.check_output(["pgrep", "-f", "uvicorn.*9201"]).decode().strip()
        if not pid_out:
            return None
        pid = pid_out.split("\n")[0].strip()
        with open("/proc/%s/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return line.strip()
    except:
        pass
    return "unknown"

def main():
    log("=== PHASE 3: LONG-SESSION SIMULATION v2 ===")
    log("Session: %s | Turns: %d | Tok/turn: ~%d | Target cum: ~%d" % (SESSION_ID, TOTAL_TURNS, TOKENS_PER_TURN, TOTAL_TURNS * TOKENS_PER_TURN))
    
    MAX_MSGS = 10
    messages = [{"role": "system", "content": "Brief responses only."}]
    cumulative = 0
    results = []
    ok_count = 0
    err_count = 0
    
    for turn in range(1, TOTAL_TURNS + 1):
        filler = gen_filler(turn, TOKENS_PER_TURN)
        messages.append({"role": "user", "content": filler})
        
        if len(messages) > MAX_MSGS + 1:
            messages = [messages[0]] + messages[-MAX_MSGS:]
        
        result = make_request(messages, max_tokens=15)
        
        if result["status"] == 200:
            usage = result["data"].get("usage", {})
            pt = usage.get("prompt_tokens", 0)
            ct = usage.get("completion_tokens", 0)
            tt = usage.get("total_tokens", 0)
            cumulative += tt
            ok_count += 1
            choices = result["data"].get("choices", [])
            ac = choices[0]["message"]["content"] if choices else ""
            messages.append({"role": "assistant", "content": ac})
            results.append({"t": turn, "pt": pt, "ct": ct, "tt": tt, "cum": cumulative, "ok": True})
            
            if turn % CHECK_INTERVAL == 0:
                rss = get_proxy_rss()
                log("T%d/%d: cum=%dok ok=%d err=%d rss=%s" % (turn, TOTAL_TURNS, cumulative, ok_count, err_count, rss))
        else:
            err_count += 1
            results.append({"t": turn, "ok": False, "err": result.get("error", "?")})
            log("T%d: ERR %s" % (turn, result.get("error", "?")))
            time.sleep(2)
            r2 = make_request(messages, max_tokens=15)
            if r2["status"] == 200:
                log("T%d: RECOVERED" % turn)
                results[-1]["rec"] = True
                usage2 = r2["data"].get("usage", {})
                cumulative += usage2.get("total_tokens", 0)
                ok_count += 1
                err_count -= 1
        
        time.sleep(0.2)
    
    log("=== DONE === T=%d ok=%d err=%d cum=%d" % (TOTAL_TURNS, ok_count, err_count, cumulative))
    with open(OUTPUT_FILE, "w") as f:
        json.dump({"sum": {"turns": TOTAL_TURNS, "ok": ok_count, "err": err_count, "cum": cumulative}, "rows": results}, f, indent=2)
    log("Saved: %s" % OUTPUT_FILE)

if __name__ == "__main__":
    main()
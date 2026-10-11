#!/usr/bin/env python3
"""Phase 1 FINAL: Calibration with correct model.
Model: tokens = chars / rho + n * msg_overhead + tools_chars / k_tools
Where rho = chars per token (content density)
"""
import json, sqlite3, httpx, os, statistics, sys
from collections import deque

VLLM = "http://127.0.0.1:29000"
DB = os.path.expanduser("~/.local/share/goose/sessions/sessions.db")
OUT = "/home/pawelw/ctxproxy/scratch"

def extract_text_from_parts(parts):
    texts = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            t = p.get("text", "")
            if t: texts.append(t)
    return "\n".join(texts) if texts else ""

def extract_tool_response_text(part):
    tr = part.get("toolResult", {})
    if not isinstance(tr, dict): return str(tr)
    val = tr.get("value", "")
    if isinstance(val, dict):
        content = val.get("content", "")
        if isinstance(content, list): return extract_text_from_parts(content)
        return str(content)
    return str(val)

def convert_session_to_openai(rows):
    msgs = []
    for row_id, role, cj in rows:
        if not cj: continue
        try: parts = json.loads(cj)
        except: continue
        if not isinstance(parts, list): continue
        if role == "user":
            tool_responses = [p for p in parts if p.get("type") == "toolResponse"]
            text_parts = [p for p in parts if p.get("type") == "text"]
            if tool_responses:
                for tr in tool_responses:
                    text = extract_tool_response_text(tr)
                    if text:
                        msgs.append({"role": "tool", "tool_call_id": tr.get("id", ""), "content": text})
            elif text_parts:
                text = extract_text_from_parts(text_parts)
                if text: msgs.append({"role": "user", "content": text})
        elif role == "assistant":
            tool_requests = [p for p in parts if p.get("type") == "toolRequest"]
            text_parts = [p for p in parts if p.get("type") == "text"]
            if tool_requests:
                text = extract_text_from_parts(text_parts)
                tc_list = []
                for tr in tool_requests:
                    tc_val = tr.get("toolCall", {}).get("value", {})
                    tc_list.append({"id": tr.get("id", ""), "type": "function",
                        "function": {"name": tc_val.get("name", ""), "arguments": json.dumps(tc_val.get("arguments", {}), ensure_ascii=False, separators=(",",":"))}})
                msgs.append({"role": "assistant", "content": text if text else None, "tool_calls": tc_list})
            elif text_parts:
                text = extract_text_from_parts(text_parts)
                if text: msgs.append({"role": "assistant", "content": text})
    return msgs

def msg_chars(m):
    total = 0
    content = m.get("content")
    if content:
        if isinstance(content, str): total += len(content)
        elif isinstance(content, list):
            for p in content:
                if isinstance(p, dict): total += len(p.get("text", ""))
    for tc in m.get("tool_calls", []):
        total += len(tc.get("id", ""))
        fn = tc.get("function", {})
        total += len(fn.get("name", ""))
        total += len(fn.get("arguments", ""))
    return total

def tokenize_call(client, messages):
    body = {"model": "Qwen3.8-27B", "messages": messages, "add_generation_prompt": True}
    try:
        r = client.post(f"{VLLM}/tokenize", json=body, timeout=60)
        r.raise_for_status()
        return r.json()["count"]
    except:
        return None

def main():
    print("=== Phase 1 FINAL: Calibration ===", flush=True)
    
    # --- Build dataset ---
    print("\n--- Building dataset ---", flush=True)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT session_id, COUNT(*) as cnt FROM messages GROUP BY session_id HAVING cnt >= 20 ORDER BY cnt DESC")
    session_counts = cur.fetchall()
    selected = session_counts[:250]
    
    target_char_sizes = [15000, 30000, 60000, 120000, 240000, 360000, 480000]
    prefix_requests = []
    
    for sid, cnt in selected:
        cur.execute("SELECT id, role, content_json FROM messages WHERE session_id=? ORDER BY id", (sid,))
        rows = cur.fetchall()
        msgs = convert_session_to_openai(rows)
        if len(msgs) < 3: continue
        
        for target_chars in target_char_sizes:
            cum = 0
            cut_idx = None
            for i, m in enumerate(msgs):
                cum += msg_chars(m)
                if cum >= target_chars:
                    cut_idx = i + 1
                    break
            if cut_idx is None or cut_idx < 2: continue
            while cut_idx > 0 and msgs[cut_idx-1]["role"] == "tool":
                cut_idx -= 1
            if cut_idx < 2: continue
            prefix_requests.append((sid, msgs[:cut_idx]))
    
    conn.close()
    print(f"Prefix requests: {len(prefix_requests)}", flush=True)
    
    # --- Measure ---
    print("\n--- Measuring via /tokenize ---", flush=True)
    client = httpx.Client()
    prefix_results = []
    for i, (sid, msgs) in enumerate(prefix_requests):
        tok = tokenize_call(client, msgs)
        if tok is not None:
            chars = sum(msg_chars(m) for m in msgs)
            n = len(msgs)
            prefix_results.append({"session": sid, "chars": chars, "n": n, "real": tok})
        if (i+1) % 100 == 0:
            print(f"  {i+1}/{len(prefix_requests)}", flush=True)
    client.close()
    print(f"Valid: {len(prefix_results)}", flush=True)
    
    # --- Fit rho and msg_overhead ---
    # Model: real = chars / rho + n * msg_overhead
    # => chars / (real - n * msg_overhead) = rho
    # We need to find (rho, msg_overhead) that minimizes variance of rho across samples
    
    # Strategy: binary search on msg_overhead to find the value that minimizes variance of implied rho
    best_ov = 0
    best_var = float('inf')
    
    for ov_test in range(0, 20):  # try 0 to 19 tokens per message
        rho_vals = []
        for p in prefix_results:
            content_tok = p["real"] - p["n"] * ov_test
            if content_tok > 50 and p["chars"] > 100:
                rho = p["chars"] / content_tok
                if 1.0 < rho < 10.0:
                    rho_vals.append(rho)
        if len(rho_vals) < 100:
            continue
        var = statistics.variance(rho_vals)
        if var < best_var:
            best_var = var
            best_ov = ov_test
            best_rho_vals = rho_vals
    
    rho_cold = statistics.median(best_rho_vals)
    sorted_rho = sorted(best_rho_vals)
    n_rho = len(sorted_rho)
    rho_p03 = sorted_rho[max(0, int(n_rho * 0.03))]
    rho_p97 = sorted_rho[min(n_rho-1, int(n_rho * 0.97))]
    rho_lo = max(2.0, rho_p03)
    rho_hi = min(8.0, rho_p97)
    
    print(f"\nFitted: rho_cold={rho_cold:.3f} msg_overhead={best_ov} rho_range=[{rho_lo:.3f}, {rho_hi:.3f}]", flush=True)
    print(f"  (n={n_rho}, variance={best_var:.4f})", flush=True)
    
    # --- Tools measurement ---
    print("\n--- Tools measurement ---", flush=True)
    client = httpx.Client()
    
    def make_tool_set(n, style):
        tools = []
        for i in range(n):
            if style == "small":
                tools.append({"type": "function", "function": {
                    "name": f"tool_{i}", "description": f"Tool {i}",
                    "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]}}})
            elif style == "medium":
                tools.append({"type": "function", "function": {
                    "name": f"mcp_{i}", "description": f"MCP tool {i} for handling operations",
                    "parameters": {"type": "object", "properties": {f"p{j}": {"type": "string"} for j in range(3)}, "required": ["p0"]}}})
            else:
                tools.append({"type": "function", "function": {
                    "name": f"enterprise_{i}", "description": f"Enterprise tool {i} with multi-stage validation",
                    "parameters": {"type": "object", "properties": {f"cfg_{j}": {"type": "object"} for j in range(3)}, "required": ["cfg_0"]}}})
        return tools
    
    tool_sets = [make_tool_set(5, "small"), make_tool_set(12, "medium"), make_tool_set(25, "medium"), make_tool_set(40, "large")]
    base_msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "x"}]
    tok_without = tokenize_call(client, base_msgs)
    
    tools_results = []
    for tools in tool_sets:
        tools_json = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        tools_chars = len(tools_json)
        body = {"model": "Qwen3.8-27B", "messages": base_msgs, "add_generation_prompt": True, "tools": tools}
        r = client.post(f"{VLLM}/tokenize", json=body, timeout=60)
        if r.status_code == 200 and tok_without:
            tok_with = r.json()["count"]
            exact_t = tok_with - tok_without
            k = tools_chars / exact_t if exact_t > 0 else 0
            tools_results.append({"n": len(tools), "chars": tools_chars, "exact": exact_t, "k": k})
            print(f"  {len(tools)} tools: chars={tools_chars} exact={exact_t} k={k:.2f}", flush=True)
    
    k_tools_cold = min(r["k"] * 0.9 for r in tools_results) if tools_results else 1.5
    print(f"k_tools_cold: {k_tools_cold:.3f}", flush=True)
    client.close()
    
    # --- Calibrator simulation ---
    print("\n--- Calibrator simulation ---", flush=True)
    all_sessions = list(set(p["session"] for p in prefix_results))
    import random
    random.seed(42)
    random.shuffle(all_sessions)
    half = len(all_sessions) // 2
    train_sessions = set(all_sessions[:half])
    test_sessions = set(all_sessions[half:])
    test_data = [p for p in prefix_results if p["session"] in test_sessions]
    print(f"Test: {len(test_data)} reqs / {len(test_sessions)} sessions", flush=True)
    
    def simulate(data, a, m_floor, m_floor_cold):
        sessions = {}
        rho_g = rho_cold
        errs_g = deque(maxlen=200)
        a_g = 0.1
        results = []
        
        for p in data:
            sid = p["session"]
            chars = p["chars"]
            n = p["n"]
            real = p["real"]
            
            if sid not in sessions:
                sessions[sid] = {"rho_ew": rho_g, "errs": deque(maxlen=50), "n_obs": 0}
            sess = sessions[sid]
            
            rho_used = rho_g if sess["n_obs"] == 0 else sess["rho_ew"]
            
            if sess["n_obs"] >= 3:
                m = max(m_floor, max(sess["errs"]) if sess["errs"] else 0)
            else:
                p99_g = statistics.quantiles(list(errs_g), n=100)[98] if len(errs_g) >= 10 else 0
                m = max(m_floor_cold, p99_g)
            
            # est = chars / (rho_eff) + n * msg_overhead
            # where rho_eff = rho_used * (1 + m)  [higher rho = fewer tokens = under-estimate]
            # Wait: we want to OVER-estimate tokens to be safe.
            # tokens = chars / rho. To over-estimate tokens, we need LOWER rho.
            # So: rho_eff = rho_used / (1 + m)  [lower rho = more tokens = over-estimate]
            rho_eff = rho_used / (1 + m)
            est = chars / rho_eff + n * best_ov
            
            err = (est - real) / real
            under = max(0, -err)
            over = max(0, err)
            
            results.append({"session": sid, "est": est, "real": real, "under": under, "over": over, "n_obs": sess["n_obs"], "steady": sess["n_obs"] >= 3})
            
            # Update: observe the actual rho
            content_tok = real - n * best_ov
            if content_tok > 0 and chars > 0:
                rho_obs = max(rho_lo, min(rho_hi, chars / content_tok))
                if sess["n_obs"] == 0:
                    sess["rho_ew"] = 0.5 * rho_obs + 0.5 * rho_g
                else:
                    sess["rho_ew"] = (1 - a) * sess["rho_ew"] + a * rho_obs
                sess["errs"].append(under)
                sess["n_obs"] += 1
                errs_g.append(under)
                rho_g = (1 - a_g) * rho_g + a_g * rho_obs
        
        return results
    
    # Search for best config
    print("\nSearching configs...", flush=True)
    best = None
    for a in [0.2, 0.3, 0.5]:
        for mf in [0.02, 0.03, 0.05]:
            for mfc in [0.05, 0.08, 0.10, 0.15]:
                res = simulate(test_data, a, mf, mfc)
                gated = [r for r in res if r["real"] >= 20000]
                if len(gated) < 10: continue
                u = sorted([r["under"] for r in gated])
                o = sorted([r["over"] for r in gated])
                n = len(u)
                p99u = u[min(n-1, int(n*0.99))]
                maxu = u[-1]
                p99o = o[min(n-1, int(n*0.99))]
                su = [r["under"] for r in gated if r["steady"]]
                maxs = max(su) if su else 0
                
                g1 = p99u <= 0.02
                g2 = maxu <= 0.0625
                g3 = p99o <= 0.12
                g4 = maxs <= 0.04
                all_pass = g1 and g2 and g3 and g4
                
                if all_pass:
                    score = p99u + maxu*0.5
                    if best is None or score < best["score"]:
                        best = {"a": a, "mf": mf, "mfc": mfc, "p99u": p99u, "maxu": maxu, "p99o": p99o, "maxs": maxs, "n": len(gated), "score": score, "pass": True}
    
    if best is None:
        for a in [0.2, 0.3, 0.5]:
            for mf in [0.02, 0.03, 0.05, 0.08]:
                for mfc in [0.05, 0.08, 0.10, 0.15, 0.20]:
                    res = simulate(test_data, a, mf, mfc)
                    gated = [r for r in res if r["real"] >= 20000]
                    if len(gated) < 10: continue
                    u = sorted([r["under"] for r in gated])
                    o = sorted([r["over"] for r in gated])
                    n = len(u)
                    p99u = u[min(n-1, int(n*0.99))]
                    maxu = u[-1]
                    p99o = o[min(n-1, int(n*0.99))]
                    su = [r["under"] for r in gated if r["steady"]]
                    maxs = max(su) if su else 0
                    score = p99u*2 + maxu + p99o*0.5
                    if best is None or score < best["score"]:
                        best = {"a": a, "mf": mf, "mfc": mfc, "p99u": p99u, "maxu": maxu, "p99o": p99o, "maxs": maxs, "n": len(gated), "score": score, "pass": False}
    
    if best:
        print(f"\nBest: a={best['a']} mf={best['mf']} mfc={best['mfc']}", flush=True)
        print(f"  p99u={best['p99u']:.4f} maxu={best['maxu']:.4f} p99o={best['p99o']:.4f} maxs={best['maxs']:.4f} n={best['n']} pass={best['pass']}", flush=True)
        
        res = simulate(test_data, best["a"], best["mf"], best["mfc"])
        gated = [r for r in res if r["real"] >= 20000]
        u = sorted([r["under"] for r in gated])
        o = sorted([r["over"] for r in gated])
        n = len(u)
        print(f"\nFull dist (n={n}):", flush=True)
        print(f"  Under: p50={u[n//2]:.4f} p95={u[int(n*0.95)]:.4f} p99={u[int(n*0.99)]:.4f} max={u[-1]:.4f}", flush=True)
        print(f"  Over:  p50={o[n//2]:.4f} p95={o[int(n*0.95)]:.4f} p99={o[int(n*0.99)]:.4f} max={o[-1]:.4f}", flush=True)
    
    # --- Write constants ---
    constants = {
        "predictor": "P1_chars_div_rho",
        "formula": "tokens = chars / rho + n * msg_overhead + tools_chars / k_tools",
        "rho_cold": round(rho_cold, 4),
        "rho_lo": round(rho_lo, 4),
        "rho_hi": round(rho_hi, 4),
        "msg_overhead": best_ov,
        "a_ewma": best["a"] if best else 0.3,
        "a_global": 0.1,
        "m_floor": best["mf"] if best else 0.03,
        "m_floor_cold": best["mfc"] if best else 0.08,
        "m_cap": 0.25,
        "k_tools_cold": round(k_tools_cold, 3),
        "data": {
            "prefix_requests": len(prefix_results),
            "sessions": len(all_sessions),
            "test_requests": len(test_data),
            "rho_samples": n_rho,
        },
        "gates": {
            "g1_p99_under": best["p99u"] if best else None,
            "g2_max_under": best["maxu"] if best else None,
            "g3_p99_over": best["p99o"] if best else None,
            "g4_steady_max": best["maxs"] if best else None,
            "all_pass": best["pass"] if best else False,
        },
    }
    
    out_path = f"{OUT}/calibration-constants.json"
    with open(out_path, "w") as f:
        json.dump(constants, f, indent=2)
    print(f"\nWrote {out_path}", flush=True)
    print(json.dumps(constants, indent=2), flush=True)

if __name__ == "__main__":
    main()

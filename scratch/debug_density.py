#!/usr/bin/env python3
"""Debug: understand the actual chars/token relationship from our data."""
import json, sqlite3, httpx, os, statistics

VLLM = "http://127.0.0.1:29000"
DB = os.path.expanduser("~/.local/share/goose/sessions/sessions.db")

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

# Get 20 diverse sessions
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT session_id, COUNT(*) as cnt FROM messages GROUP BY session_id HAVING cnt >= 20 ORDER BY cnt DESC LIMIT 20")
sessions = cur.fetchall()

client = httpx.Client()
print(f"{'session':>12} {'msgs':>5} {'chars':>8} {'tokens':>8} {'c/t':>6} {'ov/msg':>8}")
print("-" * 60)

ratios = []
for sid, cnt in sessions:
    cur.execute("SELECT id, role, content_json FROM messages WHERE session_id=? ORDER BY id", (sid,))
    rows = cur.fetchall()
    msgs = convert_session_to_openai(rows)
    if len(msgs) < 5: continue
    
    # Take first 50 messages as a prefix
    prefix = msgs[:min(50, len(msgs))]
    chars = sum(msg_chars(m) for m in prefix)
    n = len(prefix)
    
    body = {"model": "Qwen3.8-27B", "messages": prefix, "add_generation_prompt": True}
    r = client.post(f"{VLLM}/tokenize", json=body, timeout=60)
    if r.status_code != 200:
        print(f"  ERROR: {r.status_code}")
        continue
    tok = r.json()["count"]
    
    c_per_t = chars / tok if tok > 0 else 0
    # Estimate overhead: (tok - chars/7.5) / n
    ov = (tok - chars / 7.5) / n if n > 0 else 0
    
    ratios.append((chars, tok, n, c_per_t, ov))
    print(f"{sid[:12]:>12} {n:>5} {chars:>8} {tok:>8} {c_per_t:>6.2f} {ov:>8.1f}")

client.close()
conn.close()

print("\n--- Summary ---")
c_per_t_vals = [r[3] for r in ratios]
ov_vals = [r[4] for r in ratios]
print(f"chars/token: median={statistics.median(c_per_t_vals):.2f} min={min(c_per_t_vals):.2f} max={max(c_per_t_vals):.2f}")
print(f"overhead/msg: median={statistics.median(ov_vals):.1f} min={min(ov_vals):.1f} max={max(ov_vals):.1f}")

# Now fit: tokens = chars/rho + n*overhead
# Using median chars/token as rho, and median overhead
rho = statistics.median(c_per_t_vals)
ov = statistics.median([v for v in ov_vals if v > 0]) if any(v > 0 for v in ov_vals) else 12.0
print(f"\nFitted: rho={rho:.3f} overhead={ov:.1f}")

# Verify: for each sample, predict and compare
print("\n--- Verification (rho={:.3f}, ov={:.1f}) ---".format(rho, ov))
for chars, tok, n, cpt, ov_raw in ratios:
    pred = chars / rho + n * ov
    err = (pred - tok) / tok
    print(f"  chars={chars:>7} tok={tok:>6} n={n:>3} pred={pred:>7.0f} err={err:+.2%}")

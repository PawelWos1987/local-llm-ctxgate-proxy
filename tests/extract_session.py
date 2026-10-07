"""Extract real session history from Goose DB copy into OpenAI-compatible format.
Handles Goose Code Mode where toolRequest blocks have toolCall.value.name/arguments."""
import sqlite3, json, sys, os

DB = "/home/pawelw/ctxproxy-dev/tests/sessions_copy.db"
SESSION_ID = "20261006_31"
OUT = "/home/pawelw/ctxproxy-dev/tests/real_session_20261006_31.json"

def extract_session(db_path, session_id):
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    cur.execute("SELECT id, role, content_json FROM messages WHERE session_id=? ORDER BY id", (session_id,))
    rows = cur.fetchall()
    db.close()
    
    messages = []
    for msg_id, role, cj in rows:
        data = json.loads(cj)
        if not isinstance(data, list):
            continue
        
        text_parts = []
        tool_calls = []
        tool_results = []
        
        for block in data:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            
            if btype == "text":
                t = block.get("text", "")
                if t and t.strip():
                    text_parts.append(t)
            
            elif btype == "toolRequest":
                # Code Mode: toolCall.value has name and arguments
                tc_val = block.get("toolCall", {}).get("value", {})
                name = tc_val.get("name", "unknown")
                args = tc_val.get("arguments", {})
                tc_id = block.get("id", "tc-" + str(msg_id))
                tool_calls.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, dict) else str(args)
                    }
                })
            
            elif btype == "toolResponse":
                tc_id = block.get("id", "tc-" + str(msg_id))
                # Extract content from toolResult.value.content
                tr_val = block.get("toolResult", {}).get("value", {})
                content_blocks = tr_val.get("content", [])
                flat = []
                for c in content_blocks:
                    if isinstance(c, dict):
                        flat.append(c.get("text", ""))
                    else:
                        flat.append(str(c))
                content = "\n".join(flat)
                tool_results.append({
                    "tool_call_id": tc_id,
                    "content": content[:3000]
                })
        
        if role == "assistant":
            msg = {"role": "assistant", "content": "".join(text_parts) if text_parts else ""}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
            if tool_results:
                for tr in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["content"]
                    })
        elif role == "user":
            text = "".join(text_parts).strip()
            if text:
                messages.append({"role": "user", "content": text})
            if tool_results:
                for tr in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["content"]
                    })
    
    return messages

msgs = extract_session(DB, SESSION_ID)
print(f"Extracted {len(msgs)} messages")

roles = {}
total_chars = 0
tc_count = 0
for m in msgs:
    roles[m["role"]] = roles.get(m["role"], 0) + 1
    c = m.get("content", "")
    if isinstance(c, str):
        total_chars += len(c)
    for t in m.get("tool_calls", []):
        tc_count += 1
        total_chars += len(t.get("function", {}).get("arguments", ""))

print(f"Roles: {roles}")
print(f"Tool calls: {tc_count}")
print(f"Total chars: {total_chars} ({total_chars/1024:.0f} KB)")

# Verify tool names are present
names = set()
for m in msgs:
    for t in m.get("tool_calls", []):
        names.add(t.get("function", {}).get("name", "?"))
print(f"Tool names: {sorted(names)}")

with open(OUT, "w") as f:
    json.dump(msgs, f, indent=2)
print(f"Saved to {OUT}")

# Show first 3 and last 3
for i, m in enumerate(msgs[:3]):
    c = str(m.get("content", ""))[:80]
    tc = m.get("tool_calls", [])
    tn = [t["function"]["name"] for t in tc]
    print(f"  [{i}] {m['role']}: {c!r} tools={tn}")
print("  ...")
for i, m in enumerate(msgs[-3:], len(msgs)-3):
    c = str(m.get("content", ""))[:80]
    tc = m.get("tool_calls", [])
    tn = [t["function"]["name"] for t in tc]
    print(f"  [{i}] {m['role']}: {c!r} tools={tn}")

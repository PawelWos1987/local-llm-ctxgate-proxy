#!/usr/bin/env python3
import sqlite3, json
conn = sqlite3.connect('/home/pawelw/.local/share/goose/sessions/sessions.db')
cur = conn.cursor()

# Get assistant messages with tool requests
cur.execute("SELECT role, content_json FROM messages WHERE role='assistant' AND content_json LIKE '%toolRequest%' LIMIT 3")
print("=== ASSISTANT with toolRequest ===")
for role, cj in cur.fetchall():
    data = json.loads(cj)
    for part in data:
        ptype = part.get('type','?')
        if ptype == 'toolRequest':
            print(f'  toolRequest keys: {list(part.keys())}')
            print(f'    id: {part.get("id","")[:30]}')
            print(f'    name: {part.get("name","")}')
            args = part.get('arguments', part.get('args', {}))
            print(f'    args type: {type(args).__name__} val: {repr(args)[:100]}')
        elif ptype == 'text':
            print(f'  text: {repr(part.get("text",""))[:80]}')
        elif ptype == 'thinking':
            print(f'  thinking: {repr(part.get("thinking",""))[:60]}...')
        else:
            print(f'  {ptype}: {repr(part)[:80]}')
    print()

# Get tool messages
cur.execute("SELECT role, content_json FROM messages WHERE role='tool' LIMIT 3")
print("=== TOOL messages ===")
for role, cj in cur.fetchall():
    data = json.loads(cj)
    for part in data:
        ptype = part.get('type','?')
        print(f'  type={ptype} keys={list(part.keys())}')
        if ptype == 'toolResponse':
            print(f'    id: {part.get("id","")[:30]}')
            tr = part.get('toolResult', {})
            print(f'    toolResult keys: {list(tr.keys()) if isinstance(tr,dict) else type(tr).__name__}')
            if isinstance(tr, dict):
                val = tr.get('value','')
                print(f'    value type: {type(val).__name__} repr: {repr(val)[:100]}')
        elif ptype == 'text':
            print(f'    text: {repr(part.get("text",""))[:80]}')
    print()

# Check what roles exist
cur.execute("SELECT role, COUNT(*) FROM messages GROUP BY role")
print("=== Role counts ===")
for r, c in cur.fetchall():
    print(f'  {r}: {c}')

conn.close()

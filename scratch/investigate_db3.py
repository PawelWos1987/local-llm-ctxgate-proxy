#!/usr/bin/env python3
import sqlite3, json
conn = sqlite3.connect('/home/pawelw/.local/share/goose/sessions/sessions.db')
cur = conn.cursor()

# Get the toolCall structure
cur.execute("SELECT content_json FROM messages WHERE role='assistant' AND content_json LIKE '%toolRequest%' LIMIT 2")
print("=== toolCall structure ===")
for (cj,) in cur.fetchall():
    data = json.loads(cj)
    for part in data:
        if part.get('type') == 'toolRequest':
            tc = part.get('toolCall', {})
            print(f'  toolCall keys: {list(tc.keys()) if isinstance(tc,dict) else type(tc).__name__}')
            if isinstance(tc, dict):
                for k,v in tc.items():
                    print(f'    {k}: {repr(v)[:120]}')
            print()

# Get toolResponse structure in user messages
cur.execute("SELECT content_json FROM messages WHERE role='user' AND content_json LIKE '%toolResponse%' LIMIT 3")
print("=== toolResponse in user msgs ===")
for (cj,) in cur.fetchall():
    data = json.loads(cj)
    for part in data:
        ptype = part.get('type','?')
        if ptype == 'toolResponse':
            print(f'  keys: {list(part.keys())}')
            print(f'  id: {part.get("id","")[:40]}')
            tr = part.get('toolResult', {})
            if isinstance(tr, dict):
                print(f'  toolResult keys: {list(tr.keys())}')
                val = tr.get('value','')
                print(f'  value type: {type(val).__name__}')
                print(f'  value repr: {repr(val)[:150]}')
            else:
                print(f'  toolResult: {repr(tr)[:100]}')
        elif ptype == 'text':
            print(f'  [also has text: {repr(part.get("text",""))[:60]}]')
    print()

# How many user messages have toolResponse?
cur.execute("SELECT COUNT(*) FROM messages WHERE role='user' AND content_json LIKE '%toolResponse%'")
print(f'User msgs with toolResponse: {cur.fetchone()[0]}')

# How many assistant have toolRequest?
cur.execute("SELECT COUNT(*) FROM messages WHERE role='assistant' AND content_json LIKE '%toolRequest%'")
print(f'Assistant msgs with toolRequest: {cur.fetchone()[0]}')

conn.close()

#!/usr/bin/env python3
import sqlite3, json
conn = sqlite3.connect('/home/pawelw/.local/share/goose/sessions/sessions.db')
cur = conn.cursor()

cur.execute('PRAGMA table_info(messages)')
print('Messages schema:')
for row in cur.fetchall():
    print(f'  {row}')

print()
cur.execute("SELECT role, content_json FROM messages WHERE content_json IS NOT NULL AND length(content_json) > 10 LIMIT 8")
for role, cj in cur.fetchall():
    try:
        data = json.loads(cj)
        t = type(data).__name__
        if isinstance(data, dict):
            keys = list(data.keys())
            print(f'role={role} type=dict keys={keys}')
            for k in keys[:5]:
                v = data[k]
                vr = repr(v)[:120]
                print(f'  {k}: {vr}')
        elif isinstance(data, list):
            print(f'role={role} type=list len={len(data)}')
            if data:
                print(f'  first: {repr(data[0])[:150]}')
        else:
            print(f'role={role} type={t} val={repr(data)[:100]}')
    except Exception as e:
        print(f'role={role} PARSE_FAIL: {str(e)[:50]} raw={cj[:80]}')
    print()

conn.close()

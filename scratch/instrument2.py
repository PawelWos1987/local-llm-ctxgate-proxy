
import re

with open("/home/pawelw/ctxproxy/scratch/app_sandbox.py", "r") as f:
    content = f.read()

# Use targeted string replacements that preserve indentation

# 1. Per-request diagnostic: insert after the session_key assignment line
# The line is:        session_key = f"goose:{_goose_meta['id']}"
old1 = '        session_key = f"goose:{_goose_meta[\'id\']}"'
new1 = old1 + '''
        # [DIAG] Per-request characterization
        _diag_sys = ""
        for _dm in messages:
            if _dm.get("role") == "system":
                _dc = _dm.get("content", "")
                if isinstance(_dc, list):
                    _dc = " ".join(p.get("text","") for p in _dc if isinstance(p, dict))
                _diag_sys = _dc
                break
        _diag_sha = hashlib.sha1(_diag_sys.encode()).hexdigest()[:8] if _diag_sys else "none"
        _diag_has_tools = "tools" in body and bool(body.get("tools"))
        _diag_stream = bool(body.get("stream"))
        _diag_maxtok = body.get("max_tokens", 0)
        log.info("[DIAG] sid=%s sys_sha=%s msgs=%d tools=%s stream=%s maxtok=%s sk=%s",
                 _goose_meta["id"], _diag_sha, len(messages), _diag_has_tools, _diag_stream, _diag_maxtok, session_key)'''

if old1 in content:
    content = content.replace(old1, new1, 1)
    print("1. Per-request diag: OK")
else:
    print("1. Per-request diag: NOT FOUND")

# 2. _window_load_tried.add(sk) - add log after it (match its indentation)
old2 = '            _window_load_tried.add(sk)'
new2 = old2 + '\n            log.info("[DIAG] _window_load_tried.add(%s)", sk)'
if old2 in content:
    content = content.replace(old2, new2, 1)
    print("2. _window_load_tried: OK")
else:
    print("2. _window_load_tried: NOT FOUND")

# 3. session_compactions[sk] = ws (from DB load) - the one inside build_context
# Pattern: "                ws = loaded\n                session_compactions[sk] = ws"
old3 = '                ws = loaded\n                session_compactions[sk] = ws'
new3 = '                ws = loaded\n                session_compactions[sk] = ws\n                log.info("[DIAG] session_compactions SET from DB: sk=%s cut=%s st=%s", sk, ws.get("cut"), ws.get("summarized_through"))'
if old3 in content:
    content = content.replace(old3, new3, 1)
    print("3. session_compactions SET from DB: OK")
else:
    print("3. session_compactions SET from DB: NOT FOUND")

# 4. Under-limit pop (H3): "            if sk:\n                session_compactions.pop(sk, None)"
# This is in the slow path when total <= MAX_INPUT
old4 = '            if sk:\n                session_compactions.pop(sk, None)\n            return messages'
new4 = '            if sk:\n                log.warning("[DIAG] H3-POP under-limit: sk=%s total=%d", sk, total)\n                session_compactions.pop(sk, None)\n            return messages'
if old4 in content:
    content = content.replace(old4, new4, 1)
    print("4. H3 under-limit pop: OK")
else:
    print("4. H3 under-limit pop: NOT FOUND")

# 5. Slow path set: "        session_compactions[sk] = new_ws"
old5 = '        session_compactions[sk] = new_ws'
new5 = '        session_compactions[sk] = new_ws\n        log.info("[DIAG] session_compactions SET slow-path: sk=%s cut=%d st_start=%d", sk, cut, st_start)'
if old5 in content:
    content = content.replace(old5, new5, 1)
    print("5. Slow path set: OK")
else:
    print("5. Slow path set: NOT FOUND")

# 6. Seed freeze (first time)
old6 = '                session_seeds[session_key] = [dict(m) for m in built[:3]]\n                log.info("Seed frozen session=%s (3 msgs)", session_key)'
new6 = '                session_seeds[session_key] = [dict(m) for m in built[:3]]\n                log.info("[DIAG] SEED-FREEZE: sk=%s", session_key)\n                log.info("Seed frozen session=%s (3 msgs)", session_key)'
if old6 in content:
    content = content.replace(old6, new6, 1)
    print("6. Seed freeze: OK")
else:
    print("6. Seed freeze: NOT FOUND")

# 7. Seed re-freeze (H2 flap) - add log after the SEED CHANGED warning
old7 = '                        log.warning("SEED CHANGED session=%s pos=%d - re-freezing (deliberate miss)", session_key, i)'
new7 = '                        log.warning("SEED CHANGED session=%s pos=%d - re-freezing (deliberate miss)", session_key, i)\n                        log.warning("[DIAG] H2-SEED-FLAP: sk=%s pos=%d", session_key, i)'
if old7 in content:
    content = content.replace(old7, new7, 1)
    print("7. H2 seed flap: OK")
else:
    print("7. H2 seed flap: NOT FOUND")

# 8. Pop in seed flap: "                        session_compactions.pop(session_key, None)"
old8 = '                        session_compactions.pop(session_key, None)'
new8 = '                        log.warning("[DIAG] H2-POP seed-flap: sk=%s", session_key)\n                        session_compactions.pop(session_key, None)'
if old8 in content:
    content = content.replace(old8, new8, 1)
    print("8. H2 pop seed-flap: OK")
else:
    print("8. H2 pop seed-flap: NOT FOUND")

# 9. _fire_and_forget_extract entry
old9 = 'async def _fire_and_forget_extract'
new9 = 'async def _fire_and_forget_extract'
# Find the full def line and add a log after the def
lines = content.split('\n')
for i, line in enumerate(lines):
    if line.startswith('async def _fire_and_forget_extract'):
        # Find the next non-empty, non-decorator line (the first line of the body)
        j = i + 1
        while j < len(lines) and (lines[j].strip() == '' or lines[j].strip().startswith('#') or lines[j].strip().startswith('@')):
            j += 1
        if j < len(lines):
            indent = len(lines[j]) - len(lines[j].lstrip())
            lines.insert(j, ' ' * indent + 'log.info("[DIAG] fire_and_forget_extract: task=%s sk=%s", task_uuid, session_key)')
        break
content = '\n'.join(lines)
print("9. fire_and_forget_extract: OK")

with open("/home/pawelw/ctxproxy/scratch/app_sandbox.py", "w") as f:
    f.write(content)

print(f"\nDone. Total lines: {len(content.split(chr(10)))}")

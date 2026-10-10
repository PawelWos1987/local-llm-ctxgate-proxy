
import re

with open("/home/pawelw/ctxproxy/scratch/app_sandbox.py", "r") as f:
    lines = f.read().split("\n")

insertions = []

for i, line in enumerate(lines):
    # 1. After session_key assignment - add per-request diagnostic
    if "session_key = f" in line and "goose:" in line and "_goose_meta" in line:
        insertions.append((i+1, '''
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
                 _goose_meta["id"], _diag_sha, len(messages), _diag_has_tools, _diag_stream, _diag_maxtok, session_key)'''))
        break

# 2. _window_load_tried.add(sk)
for i, line in enumerate(lines):
    if "_window_load_tried.add(sk)" in line:
        insertions.append((i+1, '        log.info("[DIAG] _window_load_tried.add(%s)", sk)'))
        break

# 3. session_compactions[sk] = ws (from DB load in build_context)
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped == "session_compactions[sk] = ws":
        # Check if near window_load context
        context = "\n".join(lines[max(0,i-5):i+1])
        if "loaded" in context or "_window_load" in context:
            insertions.append((i+1, '                log.info("[DIAG] session_compactions SET from DB: sk=%s cut=%s st=%s", sk, ws.get("cut"), ws.get("summarized_through"))'))
            break

# 4. Under-limit pop (H3)
for i, line in enumerate(lines):
    if "session_compactions.pop(sk, None)" in line:
        context = "\n".join(lines[max(0,i-5):i+1])
        if "MAX_INPUT" in context and "total" in context:
            insertions.append((i+1, '                log.warning("[DIAG] H3-POP under-limit: sk=%s total=%d", sk, total)'))
            break

# 5. Slow path set
for i, line in enumerate(lines):
    if "session_compactions[sk] = new_ws" in line:
        insertions.append((i+1, '        log.info("[DIAG] session_compactions SET slow-path: sk=%s cut=%d st_start=%d", sk, cut, st_start)'))
        break

# 6. Seed freeze (first time)
for i, line in enumerate(lines):
    if "session_seeds[session_key] = [dict(m) for m in built[:3]]" in line:
        context = "\n".join(lines[max(0,i-5):i+1])
        if "not in session_seeds" in context:
            insertions.append((i+1, '                log.info("[DIAG] SEED-FREEZE: sk=%s", session_key)'))
            break

# 7. Seed re-freeze (H2 flap)
for i, line in enumerate(lines):
    if "SEED CHANGED" in line and "re-freezing" in line:
        insertions.append((i+1, '                        log.warning("[DIAG] H2-SEED-FLAP: sk=%s pos=%d", session_key, i)'))
        break

# 8. Pop in seed flap area
for i, line in enumerate(lines):
    if "session_compactions.pop(session_key, None)" in line:
        context = "\n".join(lines[max(0,i-5):i+1])
        if "SEED CHANGED" in context or "re-freezing" in context:
            insertions.append((i+1, '                        log.warning("[DIAG] H2-POP seed-flap: sk=%s", session_key)'))
            break

# 9. _fire_and_forget_extract entry
for i, line in enumerate(lines):
    if "async def _fire_and_forget_extract" in line:
        insertions.append((i+1, '    log.info("[DIAG] fire_and_forget_extract: task=%s sk=%s", task_uuid, session_key)'))
        break

# Apply insertions in reverse order to preserve indices
insertions.sort(key=lambda x: x[0], reverse=True)
for idx, text in insertions:
    lines.insert(idx, text)

with open("/home/pawelw/ctxproxy/scratch/app_sandbox.py", "w") as f:
    f.write("\n".join(lines))

print(f"Done. {len(insertions)} insertions. New line count: {len(lines)}")

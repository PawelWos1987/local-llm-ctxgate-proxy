
import sys

with open("/home/pawelw/ctxproxy/proxy/app.py", "r") as f:
    content = f.read()

# === CHANGE 1: Replace _fp_from_messages ===
old_fp = '''def _fp_from_messages(messages: list) -> str:
    sys_text = ""
    user_text = ""
    for m in messages:
        role = m.get("role", "")
        if role == "system" and not sys_text:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict))
            sys_text = c or ""
        elif role == "user" and sys_text and not user_text:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict))
            user_text = c or ""
            break
    return _fp_from_parts(sys_text, user_text)'''

new_fp = '''def _fp_from_messages(messages: list) -> str:
    """Fingerprint over the FIRST user message only. Goose does
    not persist a system message to sessions.db, so system+user
    cannot be used."""
    user_text = ""
    for m in messages:
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict))
            user_text = c or ""
            break
    if not user_text:
        return ""
    return hashlib.sha256(user_text.encode()).hexdigest()[:16]'''

if old_fp not in content:
    print("ERROR: CHANGE 1 - old _fp_from_messages not found")
    sys.exit(1)
content = content.replace(old_fp, new_fp, 1)
print("CHANGE 1 applied")

# === CHANGE 2+3: Replace the matching logic in _lookup_sync ===
old_lookup = '''                sys_row = conn.execute(
                    "SELECT content_json FROM messages "
                    "WHERE session_id = ? AND role = 'system' "
                    "ORDER BY created_timestamp ASC LIMIT 1",
                    (sid,),
                ).fetchone()
                if not sys_row:
                    continue
                user_row = conn.execute(
                    "SELECT content_json FROM messages "
                    "WHERE session_id = ? AND role = 'user' "
                    "ORDER BY created_timestamp ASC LIMIT 1",
                    (sid,),
                ).fetchone()
                sys_text = _extract_text_from_content_json(
                    sys_row["content_json"])
                user_text = _extract_text_from_content_json(
                    user_row["content_json"]) if user_row else ""
                if _fp_from_parts(sys_text, user_text) != target_fp:
                    continue
                return {
                    "id": sid,
                    "uuid": str(uuid.uuid5(
                        GOOSE_SESSION_UUID_NAMESPACE, sid)),
                    "name": c["name"] or sid,
                    "session_type": c["session_type"] or "",
                    "working_dir": c["working_dir"] or "",
                    "provider_name": c["provider_name"] or "",
                }
            return None'''

new_lookup = '''                user_row = conn.execute(
                    "SELECT content_json FROM messages "
                    "WHERE session_id = ? AND role = 'user' "
                    "ORDER BY created_timestamp ASC LIMIT 1",
                    (sid,),
                ).fetchone()
                if not user_row:
                    continue
                user_text = _extract_text_from_content_json(
                    user_row["content_json"])
                if not user_text:
                    continue
                _cand_fp = hashlib.sha256(
                    user_text.encode()).hexdigest()[:16]
                if _cand_fp != target_fp:
                    continue
                _matches.append({
                    "id": sid,
                    "uuid": str(uuid.uuid5(
                        GOOSE_SESSION_UUID_NAMESPACE, sid)),
                    "name": c["name"] or sid,
                    "session_type": c["session_type"] or "",
                    "working_dir": c["working_dir"] or "",
                    "provider_name": c["provider_name"] or "",
                })
            if not _matches:
                return None
            if len(_matches) > 1:
                log.warning("GOOSE-AMBIGUOUS fp=%s matches=%d sessions=%s (picking newest)",
                            target_fp, len(_matches), [m["id"] for m in _matches[:5]])
            best = _matches[0]
            return best'''

if old_lookup not in content:
    print("ERROR: CHANGE 2+3 - old lookup block not found")
    sys.exit(1)
content = content.replace(old_lookup, new_lookup, 1)
print("CHANGE 2+3 applied")

# Add _matches = [] before the for loop
old_for = '            for c in cand_rows:\n                sid = c["id"]'
new_for = '            _matches = []\n            for c in cand_rows:\n                sid = c["id"]'

if old_for not in content:
    print("ERROR: Could not find for-loop to insert _matches")
    sys.exit(1)
content = content.replace(old_for, new_for, 1)
print("_matches initialization added")

# === CHANGE 4: Replace GOOSE-NOMATCH with GOOSE-MATCH/NOMATCH ===
old_nomatch = '''        if _goose_meta is None:
            log.debug("GOOSE-NOMATCH fallback x_sid=%s session_key=%s task=%s",
                      x_sid, session_key, task_uuid)'''

new_nomatch = '''        if _goose_meta is not None:
            log.info("GOOSE-MATCH sid=%s session_key=%s task=%s",
                     _goose_meta["id"], session_key, task_uuid)
        else:
            log.debug("GOOSE-NOMATCH fallback x_sid=%s session_key=%s task=%s",
                      x_sid, session_key, task_uuid)'''

if old_nomatch not in content:
    print("ERROR: CHANGE 4 - old GOOSE-NOMATCH block not found")
    sys.exit(1)
content = content.replace(old_nomatch, new_nomatch, 1)
print("CHANGE 4 applied")

with open("/home/pawelw/ctxproxy/proxy/app.py", "w") as f:
    f.write(content)

print("All changes written to proxy/app.py")

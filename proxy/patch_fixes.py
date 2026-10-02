#!/usr/bin/env python3
"""Apply fixes to ctxproxy app.py - uses raw strings to avoid escape issues."""
import re

with open("/home/user/ctxproxy/proxy/app.py", "r") as f:
    lines = f.readlines()

src = "".join(lines)

# ============================================================
# FIX 1: Replace extract_knowledge with quality-gated version
# ============================================================
new_extract_knowledge = r'''def extract_knowledge(session_id: str, session_key: str, messages: list) -> list:
    """Deterministic extraction of high-signal knowledge from conversation.
    
    Quality gates:
    - Keys must be >= 4 chars, not common English words, and descriptive
    - Values must be >= 20 chars (a meaningful statement, not a fragment)
    - Max 5 items per extraction batch
    """
    _STOP_KEYS = {
        "that","this","it","was","are","be","would","could","should","can","will",
        "have","has","had","do","does","did","not","no","yes","ok","okay","fine",
        "good","great","the","and","for","with","from","your","what","when","where",
        "which","how","about","there","here","been","being","were","all","any","but",
        "its","you","our","their","then","than","into","over","under","also","just",
        "only","some","such","more","most","other","out","use","using","used","make",
        "made","get","got","one","two","see","now","new","old","set","add","run",
        "test","tests","file","line","code","error","warn","info","debug","http",
        "true","false","null","none","void","return","import","class","def","if",
    }
    
    def _valid_key(k: str) -> bool:
        k = k.strip().lower()
        if len(k) < 4 or len(k) > 60:
            return False
        if k in _STOP_KEYS:
            return False
        if not _re.search(r'[a-z]', k):
            return False
        if len(k) < 6 and not _re.search(r'd', k):
            return False
        return True
    
    def _valid_value(v: str) -> bool:
        v = v.strip().rstrip('.')
        if len(v) < 20 or len(v) > 300:
            return False
        words = v.split()
        if len(words) < 3:
            return False
        if not _re.search(r'[a-zA-Z]{3,}', v):
            return False
        return True
    
    items = []
    seen = set()
    
    for m in messages:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            continue
        
        # Pattern 1: "X is/equals/set to Y" where X is a descriptive term
        for match in _re.finditer(
            r'([a-zA-Z_][a-zA-Z0-9_]{3,}(?:s+[a-zA-Z_][a-zA-Z0-9_]{2,}){0,3})s+(?:is|equals|is set to|set to|=|should be)s+(.{20,300})',
            content, _re.IGNORECASE
        ):
            k = match.group(1).strip().lower()
            v = match.group(2).strip().rstrip('.')
            if _valid_key(k) and _valid_value(v):
                dedup = (k, v[:80])
                if dedup not in seen:
                    seen.add(dedup)
                    items.append({"domain": "fact", "key": k, "value": v[:250], "importance": 5})
        
        # Pattern 2: Decisions
        for match in _re.finditer(
            r'(?:decided to|going with|will use|chose|chooses|decided on)s+(.{20,200})',
            content, _re.IGNORECASE
        ):
            v = match.group(1).strip().rstrip('.')
            if _valid_value(v):
                k = v[:50].lower()
                if _valid_key(k):
                    dedup = ("decision", v[:80])
                    if dedup not in seen:
                        seen.add(dedup)
                        items.append({"domain": "decision", "key": k, "value": v[:250], "importance": 7})
        
        # Pattern 3: Config values
        for match in _re.finditer(
            r'(port|url|model|threshold|limit|timeout|max_w+|min_w+|pool_size|batch_size)s*(?:is|=|set to|:)?s*([w./:=-]{2,100})',
            content, _re.IGNORECASE
        ):
            k = match.group(1).strip().lower()
            v = match.group(2).strip()
            if len(v) >= 2 and len(v) <= 100:
                dedup = (k, v)
                if dedup not in seen:
                    seen.add(dedup)
                    items.append({"domain": "config", "key": k, "value": v[:200], "importance": 8})
    
    return items[:5]
'''

# Find and replace extract_knowledge
start_marker = "def extract_knowledge(session_id: str, session_key: str, messages: list) -> list:"
end_marker = "async def store_knowledge"
start_idx = src.index(start_marker)
end_idx = src.index(end_marker)
src = src[:start_idx] + new_extract_knowledge + "
" + src[end_idx:]
print("FIX 1: extract_knowledge replaced")

# ============================================================
# FIX 2: Replace _memory_worker_loop with stuck-recovery version
# ============================================================
new_worker_loop = r'''async def _memory_worker_loop():
    log.info("Memory worker loop started (model=%s, url=%s)", LM_STUDIO_MODEL, LM_STUDIO_URL)
    while True:
        try:
            await asyncio.sleep(5)
            if not pool:
                continue
            # --- Recovery: reset stuck 'processing' jobs (>120s) ---
            stuck = await pool.fetch(
                "SELECT id FROM proxy.memory_jobs WHERE status='processing' AND started_at < now() - interval '120 seconds'"
            )
            for s in stuck:
                log.warning("Resetting stuck memory job %s (processing > 120s)", s["id"])
                await pool.execute(
                    "UPDATE proxy.memory_jobs SET status='failed', error='stuck_timeout', completed_at=now() WHERE id=$1",
                    s["id"]
                )
            # --- Pick up pending jobs ---
            jobs = await pool.fetch(
                "SELECT mj.id, mj.task_id, mj.event_id FROM proxy.memory_jobs mj "
                "WHERE mj.status='pending' ORDER BY mj.created_at ASC LIMIT 5"
            )
            for job in jobs:
                await pool.execute(
                    "UPDATE proxy.memory_jobs SET status='processing', started_at=now() WHERE id=$1",
                    job["id"]
                )
                try:
                    await asyncio.wait_for(
                        _process_memory_job(str(job["id"]), str(job["task_id"]), str(job["event_id"])),
                        timeout=90
                    )
                except asyncio.TimeoutError:
                    log.warning("Memory job %s timed out after 90s", job["id"])
                    await pool.execute(
                        "UPDATE proxy.memory_jobs SET status='failed', error='timeout_90s', completed_at=now() WHERE id=$1",
                        job["id"]
                    )
        except asyncio.CancelledError:
            log.info("Memory worker loop cancelled")
            break
        except Exception as e:
            log.warning("Memory worker loop error: %s", e)
            await asyncio.sleep(10)
'''

start_marker = "async def _memory_worker_loop():"
end_marker = "async def _summarize_trimmed_messages"
start_idx = src.index(start_marker)
end_idx = src.index(end_marker)
src = src[:start_idx] + new_worker_loop + "
" + src[end_idx:]
print("FIX 2: _memory_worker_loop replaced")

# ============================================================
# FIX 3: Replace _process_memory_job with retry version
# ============================================================
new_process_job = r'''async def _process_memory_job(job_id, task_uuid, event_id):
    if not pool:
        return
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            event = await pool.fetchrow("SELECT content FROM proxy.events WHERE id=$1", event_id)
            if not event:
                await pool.execute("UPDATE proxy.memory_jobs SET status='done', completed_at=now() WHERE id=$1", job_id)
                return
            wm = await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_uuid)
            current_state = wm["content"] if wm and wm["content"] else "No prior state"
            recent_mems = await pool.fetch(
                "SELECT key, value FROM proxy.memories WHERE task_id=$1 AND active=true ORDER BY updated_at DESC LIMIT 10",
                task_uuid
            )
            mem_context = ""
            if recent_mems:
                parts = []
                for r in recent_mems:
                    parts.append("- " + r["key"] + ": " + r["value"][:100])
                mem_context = "
Known memories:
" + "
".join(parts)
            user_msg = (
                "Current task state: " + current_state + "
" + mem_context +
                "

New event:
" + event["content"][:3000] +
                "

Extract durable memories and update state."
            )
            result = await _call_4b(
                [{"role": "system", "content": _MEMORY_SYSTEM_PROMPT},
                 {"role": "user", "content": user_msg}],
                max_tokens=2000, json_mode=True
            )
            if result:
                await _store_memory_actions(task_uuid, result.get("memory_actions", []), event_id)
                await _update_working_memory(task_uuid, result.get("state_update", {}))
            await pool.execute(
                "UPDATE proxy.memory_jobs SET status='done', completed_at=now(), attempts=$2 WHERE id=$1",
                job_id, attempt
            )
            log.info("Memory job %s processed (attempt %d)", job_id, attempt)
            return
        except Exception as e:
            log.warning("Memory job %s attempt %d/%d failed: %s", job_id, attempt, max_retries, e)
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)
            else:
                try:
                    await pool.execute(
                        "UPDATE proxy.memory_jobs SET status='failed', error=$2, attempts=$3, completed_at=now() WHERE id=$1",
                        job_id, str(e)[:200], attempt
                    )
                except Exception:
                    pass
                log.error("Memory job %s permanently failed after %d attempts", job_id, max_retries)
                return
'''

start_marker = "async def _process_memory_job(job_id, task_uuid, event_id):"
end_marker = "async def _memory_worker_loop"
start_idx = src.index(start_marker)
end_idx = src.index(end_marker)
src = src[:start_idx] + new_process_job + "
" + src[end_idx:]
print("FIX 3: _process_memory_job replaced")

# ============================================================
# FIX 4: Add _is_near_duplicate + fix _store_memory_actions
# ============================================================
new_store_actions = r'''def _is_near_duplicate(existing_value: str, new_value: str) -> bool:
    """Check if two memory values are near-duplicates using token overlap."""
    stop = {"the","and","for","with","this","that","from","have","will","your","what",
            "when","where","which","how","can","could","would","should","about","there",
            "here","been","being","were","was","are","is","not","all","any","but","its",
            "you","our","their","then","than","into","over","under","also","just"}
    def _sig_tokens(text):
        return set(w for w in _re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{3,}', text.lower()) if w not in stop)
    ex_toks = _sig_tokens(existing_value)
    new_toks = _sig_tokens(new_value)
    if not ex_toks or not new_toks:
        return False
    overlap = len(ex_toks & new_toks)
    return overlap >= max(2, int(0.7 * min(len(ex_toks), len(new_toks))))

async def _store_memory_actions(task_uuid, actions, source_event_id):
    if not pool or not actions:
        return
    stored = 0
    for act in actions:
        action = act.get("action", "NEW")
        if action in ("NO_CHANGE", "DUPLICATE"):
            continue
        mtype = act.get("type", "FACT")
        importance = _IMPORTANCE_MAP.get(act.get("importance", "NORMAL"), 5)
        title = act.get("title", "")[:200]
        content = act.get("content", "")[:2000]
        if not title or not content:
            continue
        if action == "NEW":
            existing = await pool.fetchrow(
                "SELECT value FROM proxy.memories WHERE task_id=$1 AND active=true AND key ILIKE $2 LIMIT 1",
                task_uuid, "%" + title[:30] + "%"
            )
            if existing and _is_near_duplicate(existing["value"], content):
                log.debug("Skipping near-duplicate memory: %s", title)
                continue
            await pool.execute(
                "INSERT INTO proxy.memories (task_id, key, value, category, importance, active, source_event_id, status, model_name) "
                "VALUES ($1,$2,$3,$4,$5,true,$6,'active',$7)",
                task_uuid, title, content, mtype, importance, source_event_id, LM_STUDIO_MODEL
            )
            stored += 1
        elif action == "UPDATE":
            row = await pool.fetchrow(
                "SELECT id, value FROM proxy.memories WHERE task_id=$1 AND key=$2 AND active=true LIMIT 1",
                task_uuid, title
            )
            if row:
                if _is_near_duplicate(row["value"], content):
                    log.debug("Skipping no-op update: %s", title)
                    continue
                await pool.execute(
                    "UPDATE proxy.memories SET value=$3, importance=$4, updated_at=now() WHERE id=$5",
                    content, importance, row["id"]
                )
            else:
                await pool.execute(
                    "INSERT INTO proxy.memories (task_id, key, value, category, importance, active, source_event_id, status, model_name) "
                    "VALUES ($1,$2,$3,$4,$5,true,$6,'active',$7)",
                    task_uuid, title, content, mtype, importance, source_event_id, LM_STUDIO_MODEL
                )
            stored += 1
        elif action == "SUPERSEDE":
            old = await pool.fetchrow(
                "SELECT id FROM proxy.memories WHERE task_id=$1 AND key=$2 AND active=true LIMIT 1",
                task_uuid, title
            )
            new_id = await pool.fetchval(
                "INSERT INTO proxy.memories (task_id, key, value, category, importance, active, source_event_id, status, model_name) "
                "VALUES ($1,$2,$3,$4,$5,true,$6,'active',$7) RETURNING id",
                task_uuid, title, content, mtype, importance, source_event_id, LM_STUDIO_MODEL
            )
            if old:
                await pool.execute(
                    "UPDATE proxy.memories SET active=false, superseded_by=$2, updated_at=now() WHERE id=$1",
                    old["id"], new_id
                )
            stored += 1
    if stored:
        log.info("Stored %d memory actions for task %s", stored, task_uuid)
'''

start_marker = "async def _store_memory_actions(task_uuid, actions, source_event_id):"
end_marker = "async def _update_working_memory"
start_idx = src.index(start_marker)
end_idx = src.index(end_marker)
src = src[:start_idx] + new_store_actions + "
" + src[end_idx:]
print("FIX 4: _store_memory_actions + _is_near_duplicate replaced")

# ============================================================
# FIX 5: Fix the session summary STATE quote bug
# ============================================================
# The original has: parts.insert(0, "STATE: ' + su['current_state']")
# which is a syntax error (mismatched quotes). Fix it.
src = src.replace(
    'parts.insert(0, "STATE: ' + su['current_state']")',
    'parts.insert(0, "STATE: " + su['current_state'])'
)
# Also fix the other occurrence
src = src.replace(
    'parts.insert(0, "STATE: ' + su['current_state'])',
    'parts.insert(0, "STATE: " + su['current_state'])'
)
print("FIX 5: STATE quote bug fixed")

# Write the file
with open("/home/user/ctxproxy/proxy/app.py", "w") as f:
    f.write(src)

print(f"
All patches applied. File size: {len(src)} chars")

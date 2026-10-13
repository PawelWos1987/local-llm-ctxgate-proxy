"""Task memory and knowledge fetching."""
import asyncio
import re as _re



async def fetch_task_memory(session_id: str, messages: list, task_uuid: str = None, wm_budget: int = 1200, mem_budget: int = 3300, total_budget: int = 6000, recap: bool = False, tenant_id: str = "") -> str:
    """Section 13/14: per-task durable memory as a SMALL CONDITIONAL supplement.

    Final-architecture rules:
      - Inject ONLY memory not already present in the current Goose context
        (Goose compaction owns short-term continuity; we add what is missing).
      - Gate on deterministic relevance signals (section 5); no LLM in the loop.
      - If nothing relevant / already in context -> inject nothing (zero overhead).
      - Bounded to a small supplement (default <= ~2k tokens total).
      - Appended to the END of the system prompt (stable prefix, small suffix).
    Priority: working state > critical durable > relevant historical.
    """
    from proxy.app import log, pool  # TODO(I4): replace with AppContext
    from proxy.db.tasks import _resolve_task
    from proxy.memory.score import _already_in_context, _context_blob, _extract_terms, _score_memory
    from proxy.summarize.slices import _fetch_session_summary
    from proxy.token.counter import count_tokens
    if not pool:
        return ""
    if task_uuid is None:
        task_uuid = await _resolve_task(session_id, create=False, tenant_id=tenant_id)
        if task_uuid is None:
            return ""
    blob = _context_blob(messages)
    terms = _extract_terms(messages)
    parts = []
    total = 0

    # 1. Working memory (current working state) - inject only if not already in context
    # 1+1b+2. Working memory + session summary + durable memories (parallel)
    async def _fetch_rel_mem():
        if recap:
            # Phase 4: recap mode - fetch ALL typed memories chronologically
            rows = await pool.fetch(
                "SELECT id, key, value, category, importance, updated_at FROM proxy.memories "
                "WHERE task_id=$1 AND active=true "
                "AND category IN ('DECISION','FAILURE','MILESTONE','TODO') "
                "ORDER BY updated_at ASC LIMIT 20",
                task_uuid
            )
            return list(rows)
        if terms:
            rows = await pool.fetch(
                "SELECT id, key, value, category, importance, updated_at FROM proxy.memories "
                "WHERE task_id=$1 AND active=true "
                "AND (key ILIKE ANY($2) OR value ILIKE ANY($2)) "
                "LIMIT 30",
                task_uuid, ["%" + t + "%" for t in list(terms)[:20]]
            )
            scored = []
            for r in rows:
                s = _score_memory(r["key"], r["value"], r.get("category", "general"), r.get("importance", 5), r.get("updated_at"), terms, blob)
                if s > 0.1:
                    scored.append((s, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [r for _, r in scored[:8]]
        return []

    wrow, summary, crit, rel = await asyncio.gather(
        pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_uuid),
        _fetch_session_summary(task_uuid, budget=1500, session_key=session_id),
        pool.fetch("SELECT id, key, value, category, importance, updated_at FROM proxy.memories WHERE task_id=$1 AND active=true AND importance=10 ORDER BY updated_at DESC LIMIT 5", task_uuid),
        _fetch_rel_mem(),
        return_exceptions=True
    )
    if isinstance(wrow, Exception):
        log.warning("Working memory fetch failed: %s", wrow)
        wrow = None
    if isinstance(summary, Exception):
        log.warning("Session summary fetch failed: %s", summary)
        summary = ""
    if isinstance(crit, Exception):
        log.warning("Critical memory fetch failed: %s", crit)
        crit = []
    if isinstance(rel, Exception):
        log.warning("Relevant memory fetch failed: %s", rel)
        rel = []

    wm_text = (wrow["content"].strip() if wrow and wrow["content"] else "")
    if wm_text and not _already_in_context(wm_text, blob):
        line = "WORKING MEMORY: " + wm_text
        t = count_tokens(line)
        if t <= wm_budget and total + t <= total_budget:
            parts.append(line)
            total += t

    if summary:  # Phase 4: digest is never suppressed by _already_in_context
        line = ("TASK STATE (background only; the NEWEST user message is the live instruction - never resume the first prompt unless the newest message asks for it. "
                "Where an older message says something is not yet done but this state or the files on disk say it is, trust this state and the disk): " + summary)
        t = count_tokens(line)
        if t <= 1500 and total + t <= total_budget:
            parts.append(line)
            total += t

    rows = list(crit) + list(rel)

    seen = set()
    injected_ids: list = []
    for row in rows:
        line = row["key"] + ": " + row["value"]
        nk = _re.sub(r'[^a-z0-9]+', ' ', line.lower()).strip()
        if nk in seen:
            continue
        if _already_in_context(line, blob):
            continue
        if wm_text and row["value"] in wm_text:
            continue
        t = count_tokens(line)
        if total + t > min(mem_budget, total_budget):
            break
        parts.append(line)
        injected_ids.append(row["id"])
        seen.add(nk)
        total += t

    # Touch-on-use: stamp last_accessed_at + score
    try:
        if injected_ids:
            await pool.execute("UPDATE proxy.memories SET last_accessed_at = now(), score = GREATEST(COALESCE(score, 0), 0.5) WHERE id = ANY($1)", injected_ids)
    except Exception as e:
        log.warning("touch-on-use update failed: %s", e)

    if not parts:
        return ""
    return "[Task Memory]\n" + "\n".join(parts)







async def fetch_relevant_knowledge(messages: list, max_items: int = 5, max_tokens: int = 500, tenant_id: str = "") -> str:
    """Fetch top-N most relevant knowledge items. Returns formatted text for system prompt injection.
    Phase 2: filters by tenant_id when provided."""
    from proxy.app import pool  # TODO(I4): replace with AppContext
    from proxy.token.counter import count_tokens
    if not pool:
        return ""
    search_terms = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        for word in _re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{3,}\b', content.lower()):
            if word not in ("the","and","for","with","this","that","from","have","will","your","what","when","where","which","how","can","could","would","should","about","there","here","been","being","were","was","are","is","not","all","any","but","its","you","our","their"):
                search_terms.add(word)
    rows = []
    try:
        _tf = "AND tenant_id = $1 " if tenant_id else ""
        if search_terms:
            terms_list = list(search_terms)[:20]
            if tenant_id:
                rows = await pool.fetch(
                    "SELECT key, value, importance FROM proxy.knowledge WHERE active = true "
                    "AND tenant_id = $1 "
                    "AND (key ILIKE ANY($2) OR value ILIKE ANY($2)) "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $3",
                    tenant_id, [f"%{t}%" for t in terms_list], max_items
                )
            else:
                rows = await pool.fetch(
                    "SELECT key, value, importance FROM proxy.knowledge WHERE active = true "
                    "AND (key ILIKE ANY($1) OR value ILIKE ANY($1)) "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $2",
                    [f"%{t}%" for t in terms_list], max_items
                )
        if not rows:
            if tenant_id:
                rows = await pool.fetch(
                    "SELECT key, value, importance FROM proxy.knowledge WHERE active = true "
                    "AND tenant_id = $1 "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $2", tenant_id, max_items
                )
            else:
                rows = await pool.fetch(
                    "SELECT key, value, importance FROM proxy.knowledge WHERE active = true "
                    "ORDER BY importance DESC, updated_at DESC LIMIT $1", max_items
                )
    except Exception:
        return ""
    if not rows:
        return ""
    parts = []
    total_tokens = 0
    for row in rows:
        line = row['key'] + ": " + row['value']
        tok = count_tokens(line)
        if total_tokens + tok > max_tokens:
            break
        parts.append(line)
        total_tokens += tok
    if not parts:
        return ""
    return "[Shared Knowledge]\n" + "\n".join(parts)





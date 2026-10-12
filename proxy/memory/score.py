"""Memory scoring and relevance detection."""
import re as _re
import datetime



def _extract_terms(messages: list) -> set:
    """Deterministic relevance signals from the current request (section 5)."""
    stop = {"the","and","for","with","this","that","from","have","will","your","what","when","where","which","how","can","could","would","should","about","there","here","been","being","were","was","are","is","not","all","any","but","its","you","our","their","then","than","into","over","under","also","just","only","some","such","more","most","other","out","use","using","used","make","made","get","got","one","two","see","now","new","old","set","add","run","test","tests"}
    terms = set()
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        for w in _re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{3,}', c.lower()):
            if w not in stop:
                terms.add(w)
    return terms







def _context_blob(messages: list) -> str:
    """Lowercased text of the current Goose context (dedupe base)."""
    parts = []
    for m in messages:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if c:
            parts.append(c.lower())
    return " ".join(parts)







def _already_in_context(text: str, blob: str) -> bool:
    """True if the memory's distinctive content is already in the Goose context.
    Deterministic (no LLM): if most significant tokens are present, it is redundant."""
    stop = {"the","and","for","with","this","that","from","have","will","your","what","when","where","which","how","can","could","would","should","about","there","here","been","being","were","was","are","is","not","all","any","but","its","you","our","their"}
    toks = [w for w in _re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{3,}', text.lower()) if w not in stop]
    uniq = set(toks)
    if not uniq:
        return False
    present = sum(1 for w in uniq if w in blob)
    return present >= max(1, int(0.6 * len(uniq)))







def _score_memory(key, value, category, importance, updated_at, context_terms, context_blob):
    if len(value) > 10 and value[:50] in context_blob:
        return 0.0
    mem_terms = set(_re.findall(r'[a-z0-9]{3,}', (key + ' ' + value).lower()))
    overlap = len(mem_terms & context_terms) / max(1, len(mem_terms)) if mem_terms else 0.0
    imp = importance / 10.0
    rec = 0.5
    if updated_at is not None:
        try:
            hours = max(0, (datetime.datetime.now(datetime.timezone.utc) - updated_at).total_seconds() / 3600)
            rec = math.exp(-hours / 72.0)
        except Exception:
            rec = 0.5
    dec = 1.0 if category == 'DECISION' else 0.0
    return 0.5 * overlap + 0.2 * imp + 0.1 * rec + 0.2 * dec




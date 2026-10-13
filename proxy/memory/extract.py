"""Knowledge extraction from conversations."""
import os
import json
import asyncio



async def _fire_and_forget_extract(session_id: str, session_key: str, messages: list, tenant_id: str = ""):
    """Background knowledge extraction - never blocks the request path.

    Step 2: at most one extraction per user turn. We keep the anchor of the
    newest user message we last extracted for each session and skip when it has
    not changed. A kill switch (CTXGATE_KNOWLEDGE_EXTRACT=0) disables it.
    """
    import proxy.app as _app
    from proxy.app import log, metrics, _newest_user_anchor, _last_extract_anchor, _sync_deliverable_summary  # TODO(I4): replace with AppContext
    from proxy.memory.store import store_knowledge
    from proxy.lm.call import _EXTRACT_SEM
    # Kill switch (read at call time so it can be toggled via env without restart)
    if os.environ.get("CTXGATE_KNOWLEDGE_EXTRACT", "0") == "0":
        return
    # Newest-user-message anchor (None => trivial/short turn, not worth it)
    anchor = _newest_user_anchor(messages)
    if anchor is None:
        metrics["extract_skipped_short"] += 1
        return
    # Once per user turn: skip if we already extracted this exact user message
    if _last_extract_anchor.get(session_key) == anchor:
        metrics["extract_skipped_same_turn"] += 1
        return
    _last_extract_anchor[session_key] = anchor
    async with _EXTRACT_SEM:
        _app._EXTRACT_IN_FLIGHT += 1
        try:
            k_items = await extract_knowledge(session_id, session_key, messages)
            if k_items:
                await store_knowledge(k_items, session_id, session_key)
        except Exception as e:
            log.warning("Knowledge extraction (background) failed: %s", e)
        finally:
            _app._EXTRACT_IN_FLIGHT -= 1
        # Background deliverable summary sync (fire-and-forget, never blocks)
        try:
            await _sync_deliverable_summary(session_id)
        except Exception:
            pass



async def extract_knowledge(session_id: str, session_key: str, messages: list) -> list:
    """Async 4B-based knowledge extraction (single call, structural filters only).

    Simplified from the old 3-5 call pipeline (generate -> verify -> regenerate
    -> re-verify). The knowledge table was empty because the multi-call pipeline
    timed out on the single-threaded 4B. Now: one generate call + deterministic
    structural filters. Queued at LM_PRI_LOW so it never blocks trim summaries.
    """
    from proxy.app import pool  # TODO(I4): replace with AppContext
    from proxy.lm.call import _call_lm_4b
    from proxy.config.constants import LM_PRI_LOW
    if not pool or not messages:
        return []
    relevant = [m for m in messages if m.get("role") in ("user", "assistant")][-6:]
    if not relevant:
        return []
    parts = []
    for m in relevant:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if c:
            parts.append(m["role"] + ": " + c[:800])
    if not parts:
        return []
    context = "\n".join(parts)

    gen_prompt = (
        "Extract 0-3 knowledge items worth preserving across sessions. "
        "Return a JSON array of {domain, key, value, importance}.\n"
        "domain: fact|decision|config|preference\n"
        "key: 2-5 descriptive words (not a single common word)\n"
        "value: 30-200 chars, a complete semantic statement (not a fragment)\n"
        "importance: 5-10\n"
        "Return [] if nothing is worth preserving.\n\n" + context
    )
    raw = await _call_lm_4b(gen_prompt, temperature=0.3, max_tokens=512, priority=LM_PRI_LOW, kind="knowledge")
    if not raw:
        return []
    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    # Deterministic structural filter (no LLM quality check)
    candidates = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        d = it.get("domain", "fact")
        k = (it.get("key") or "").strip().lower()
        v = (it.get("value") or "").strip()
        imp = min(10, max(1, int(it.get("importance", 5))))
        if len(k) < 4 or len(k) > 80:
            continue
        if len(v) < 20 or len(v) > 300:
            continue
        if d not in ("fact", "decision", "config", "preference"):
            d = "fact"
        candidates.append({"domain": d, "key": k, "value": v, "importance": imp})

    return candidates




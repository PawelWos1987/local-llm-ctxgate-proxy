"""Small utility helpers: clipping, budget fitting, recap detection, epoch keys, etc."""
import json
import logging
import re

log = logging.getLogger("ctxgate-proxy")


def _clip_at_boundary(text: str, limit: int) -> str:
    """Cut at the last whitespace/newline boundary <= limit. Never mid-word."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind("\n"), window.rfind(" "))
    if cut < limit // 2:
        cut = limit
    return window[:cut].rstrip()


def _fit_to_budget(sections: list, budget_tokens: int) -> str:
    """Fit sections into a token budget. Drop OLDEST first, never cut mid-line.

    sections: list of (priority, text) tuples, priority 0 = newest (keep), higher = older (drop first).
    Returns the joined string that fits within budget_tokens.
    """
    from proxy.token.counter import _rho_var

    if not sections:
        return ""
    sorted_secs = sorted(sections, key=lambda x: x[0])
    parts = []
    total = 0
    for prio, text in sorted_secs:
        t = len(text)
        if total + t > budget_tokens:
            if parts:
                break
            else:
                max_chars = int(budget_tokens * _rho_var.get())
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n[...truncated to fit budget...]"
                parts.append(text)
                total = len(text)
                break
        parts.append(text)
        total += t
    return "\n\n".join(parts)


def _detect_recap_intent(text: str) -> bool:
    """Deterministic recap intent detection (English + Polish)."""
    if not text:
        return False
    t = text.lower().strip()
    en_patterns = [
        r"^what did we (do|make|build|create|finish)",
        r"^summarize (the )?session",
        r"^recap",
        r"^status (report|update|check)?$",
        r"^what (is|are) (done|finished|completed)",
        r"^what have we (done|finished|completed)",
        r"^give me a (summary|recap|status)",
        r"^what (did|has) the (agent|model) (do|finish|complete)",
    ]
    pl_patterns = [
        r"^co zrobili",
        r"^podsumuj",
        r"^co jest zrobione",
        r"^co zostalo zrobione",
        r"^status (pracy|projektu|sesji)?$",
        r"^daj (mi )?(podsumowanie|recap|status)",
        r"^co sie stalo",
    ]
    for p in en_patterns + pl_patterns:
        if re.search(p, t):
            return True
    return False


def _compute_epoch_key(session_key: str, ws: dict, newest_user_anchor: str, recap: bool) -> str:
    """Compute the epoch key for injection block caching."""
    cut = ws.get("cut", 0) if ws else 0
    return session_key + "|" + str(cut) + "|" + newest_user_anchor + "|" + ("R" if recap else "N")


def _note_tok() -> int:
    """Char cost of the note (lazy + cached; never compute at import time)."""
    from proxy.app import _NOTE_TOK_CACHE, COMPACTION_NOTE
    import proxy.app as _app
    if _app._NOTE_TOK_CACHE is None:
        _app._NOTE_TOK_CACHE = max(1, len(chr(10) * 2 + COMPACTION_NOTE)) if COMPACTION_NOTE else 0
    return _app._NOTE_TOK_CACHE


def _with_compaction_note(messages: list) -> list:
    """Return messages with COMPACTION_NOTE appended to message 0 (system).
    Never mutates the input dicts. Idempotent."""
    from proxy.app import COMPACTION_NOTE
    from proxy.window.state import _norm_content

    if not COMPACTION_NOTE or not messages:
        return messages
    first = messages[0]
    if first.get("role") == "system":
        c = first.get("content")
        text = c if isinstance(c, str) else _norm_content(c)
        if text.endswith(COMPACTION_NOTE):
            return messages
        new_first = dict(first)
        new_first["content"] = (text + chr(10) * 2 + COMPACTION_NOTE) if text else COMPACTION_NOTE
        return [new_first] + list(messages[1:])
    return [{"role": "system", "content": COMPACTION_NOTE}] + list(messages)


def _eviction_priority(msg: dict, idx: int, total: int) -> int:
    """Eviction priority: 0=verbose tool, 1=old turn, 2=summary, 3=optional.
    Returns -1 for never-evict (system prefix, live prompt)."""
    role = msg.get('role', '')
    if role == 'system':
        return -1
    if role == 'user' and idx == total - 1:
        return -1
    if role == 'tool':
        c = msg.get('content', '')
        if isinstance(c, str) and len(c) > 2500:
            return 0
        return 1
    if role == 'user':
        return 1
    if role == 'assistant':
        if msg.get('tool_calls'):
            return 1
        return 2
    return 3


def ctxgate_meta(truncated: bool, reason: str, continuations_used: int,
                total_output_tokens: int, tool_calls_complete: bool,
                tool_calls_emitted: int, tool_call_truncated: bool = False) -> dict:
    """Build the structured ctxgate metadata dict for SSE final chunks."""
    meta = {
        "ctxgate": {
            "truncated": truncated,
            "reason": reason,
            "continuations_used": continuations_used,
            "total_output_tokens": total_output_tokens,
            "tool_calls_complete": tool_calls_complete,
            "tool_calls_emitted": tool_calls_emitted,
        }
    }
    if tool_call_truncated:
        meta["ctxgate"]["tool_call_truncated"] = True
    return meta


def explain_status(status: str, detail: str = "") -> str:
    """Deterministic human-readable explanation for a call status (no LLM)."""
    s = (status or "").lower()
    if not s or s == "ok":
        return ""
    if "400" in s:
        if detail:
            try:
                d = json.loads(detail)
                msg = d.get("message") if isinstance(d, dict) else None
                if msg:
                    return "vLLM HTTP 400 - " + str(msg)[:200]
            except Exception:
                pass
            return "vLLM HTTP 400 - request rejected (context / params / malformed body)"
        return "vLLM HTTP 400 - request rejected (context / params / malformed body)"
    if "404" in s:
        return "HTTP 404 - model not found on vLLM"
    if "422" in s:
        return "HTTP 422 - validation error on request body"
    if "429" in s:
        return "HTTP 429 - vLLM rate limited / overloaded"
    if "500" in s:
        return "vLLM HTTP 500 - internal server error"
    if "502" in s or "503" in s:
        return "vLLM HTTP 502/503 - upstream unavailable"
    if "504" in s or s == "timeout":
        return "Timeout - vLLM did not respond within 300s (cold APC / long generation)"
    if s == "error":
        return "Proxy error - exception while forwarding to vLLM"
    return s.replace("_", " ")


def _newest_user_anchor(messages: list) -> str | None:
    """Anchor of the newest user message, ignoring <turn-context> boilerplate."""
    from proxy.app import KNOWLEDGE_EXTRACT_MIN_CHARS
    from proxy.window.state import _msg_anchor

    newest = None
    for m in messages:
        if m.get("role") == "user":
            newest = m
    if newest is None:
        return None
    c = newest.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    c = str(c or "")
    c = re.sub(r"<turn-context>.*?</turn-context>", "", c, flags=re.DOTALL).strip()
    if len(c) < KNOWLEDGE_EXTRACT_MIN_CHARS:
        return None
    return _msg_anchor(newest)

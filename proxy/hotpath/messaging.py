"""Message manipulation: repair, sanitize, strip, loop detection, truncation classification."""
import json
import logging
import re

log = logging.getLogger("ctxgate-proxy")


def _repair_dangling_tool_calls(msgs: list) -> list:
    """Return a copy of msgs with dangling tool_calls removed.

    A tool_call is "dangling" if its tool_call_id has no matching
    role="tool" message later in the list. This happens when the seed
    (first 3 messages) includes an assistant message with a tool_call
    whose result was cut by the window.

    - Pure function: never mutates the input.
    - Idempotent: running it twice gives the same result.
    - Byte-identical: same input always produces same output.
    - Only affects assistant messages with dangling tool_calls.
    - If all tool_calls are dangling, the key is removed.
    - If content is empty after removal, a placeholder is inserted.

    Env: CTXGATE_REPAIR_DANGLING_TOOLCALLS (default 1).
    """
    from proxy.app import REPAIR_DANGLING_TOOLCALLS, _DANGLING_PLACEHOLDER

    if not REPAIR_DANGLING_TOOLCALLS:
        return msgs
    if not msgs:
        return msgs

    resolved_ids = set()
    for m in msgs:
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            if tcid:
                resolved_ids.add(tcid)

    needs_repair = False
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id", "")
                if tcid and tcid not in resolved_ids:
                    needs_repair = True
                    break
            if needs_repair:
                break

    if not needs_repair:
        return msgs

    result = []
    for m in msgs:
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            result.append(m)
            continue

        kept_calls = []
        for tc in m["tool_calls"]:
            tcid = tc.get("id", "")
            if tcid and tcid not in resolved_ids:
                continue
            kept_calls.append(tc)

        if len(kept_calls) == len(m["tool_calls"]):
            result.append(m)
            continue

        new_m = dict(m)
        if kept_calls:
            new_m["tool_calls"] = kept_calls
        else:
            new_m.pop("tool_calls", None)
            content = new_m.get("content", "")
            if not content or (isinstance(content, str) and not content.strip()):
                new_m["content"] = _DANGLING_PLACEHOLDER
        result.append(new_m)

    return result


def _detect_loop(text: str) -> int:
    """Detect a repetition loop in the recent tail of generated text.

    Returns the loop period in chars (>0) when a loop is found, else 0.
    """
    from proxy.app import LOOP_TAIL, LOOP_REPEATS, LOOP_SENTENCE_REPEATS

    if not text or len(text) < 40:
        return 0
    tail = text[-LOOP_TAIL:]
    probe = tail[-40:]
    prev = tail.rfind(probe, 0, len(tail) - 40)
    p = (len(tail) - 40) - prev if prev >= 0 else 0
    if p >= 20 and len(tail) >= 4 * p and tail[-4 * p:] == tail[-p:] * LOOP_REPEATS:
        unit = tail[-p:]
        if unit.count("\n") <= 1:
            return p
    sents = [re.sub(r"\s+", " ", x.strip().lower()) for x in re.split(r"(?<=[.!?])\s+|\n+", tail[-4000:])]
    counts = {}
    for x in sents:
        if len(x) >= 30:
            counts[x] = counts.get(x, 0) + 1
            if counts[x] >= LOOP_SENTENCE_REPEATS:
                return len(x)
    words = re.findall(r"[a-zA-Z\u00C0-\u024F]{3,}", tail[-4000:].lower())
    if len(words) >= 10:
        run_len = 1
        for i in range(1, len(words)):
            if words[i] == words[i - 1]:
                run_len += 1
                if run_len >= 10:
                    return len(words[i])
            else:
                run_len = 1
    return 0


def _classify_truncation(finish_reason: str, content: str, reasoning_content: str, tool_calls: list) -> str:
    from proxy.app import count_tokens
    reasoning_tok = count_tokens(reasoning_content or "")
    content_len = len(content or "")
    if finish_reason == "length":
        if reasoning_tok >= 12000 and content_len < 50:
            return "reasoning_overflow"
        return "content_truncation"
    if finish_reason == "tool_calls":
        for tc in (tool_calls or []):
            try:
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    json.loads(args)
            except (json.JSONDecodeError, ValueError):
                return "tool_call_truncation"
        return "none"
    return "none"


def sanitize_tool_calls(message: dict) -> tuple:
    tc = message.get("tool_calls")
    if not tc:
        return message, False
    valid = []
    all_valid = True
    for t in tc:
        if not isinstance(t, dict):
            all_valid = False
            break
        if "id" not in t or "type" not in t or "function" not in t:
            all_valid = False
            break
        fn = t.get("function", {})
        if "name" not in fn or "arguments" not in fn:
            all_valid = False
            break
        args_str = fn["arguments"]
        if isinstance(args_str, str):
            try:
                json.loads(args_str)
            except (json.JSONDecodeError, ValueError):
                all_valid = False
                break
        elif not isinstance(args_str, dict):
            all_valid = False
            break
        valid.append(t)
    if all_valid:
        return message, False
    log.warning("D9: stripped malformed tool_calls from assistant message id=%s", message.get("id", "?"))
    cleaned = {k: v for k, v in message.items() if k != "tool_calls"}
    if not cleaned.get("content"):
        cleaned["content"] = ""
    return cleaned, True


def strip_reasoning(messages: list) -> list:
    """Canonical: remove hidden reasoning fields from assistant messages."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and ("reasoning" in m or "reasoning_content" in m):
            m = {k: v for k, v in m.items() if k not in ("reasoning", "reasoning_content")}
        out.append(dict(m))
    return out

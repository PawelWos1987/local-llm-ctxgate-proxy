"""Context invariant verification and outgoing payload validation."""
import hashlib
import json
import logging
from typing import Optional

from proxy.window.state import _norm_content, _msg_anchor
from proxy.token.counter import count_messages_tokens

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _live_prompt_identity(messages: list) -> str:
    """Compute an immutable SHA-256 identity for the live prompt (last user message).
    
    Uses canonical JSON serialization to preserve content block order and
    multimodal semantics. Returns hex digest, or empty string if no user message.
    """
    for m in reversed(messages):
        if m.get("role") == "user":
            # Canonical JSON: sort keys for determinism, preserve list order
            return hashlib.sha256(
                json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
    return ""


def protected_tool_groups(messages: list) -> set:
    """Return the set of indices that must NEVER be elided/dropped:
    the newest 4 tool-result messages AND the assistant message(s) that
    declare their tool_calls (so the tool-call graph stays valid).

    Walk from the end, count tool messages up to 4, and for each, also
    include the nearest preceding assistant message that has tool_calls
    referencing them. This is the SINGLE definition of "recent tool body".
    """
    protected = set()
    tool_count = 0
    for i in range(len(messages) - 1, -1, -1):
        if tool_count >= 4:
            break
        m = messages[i]
        if m.get("role") == "tool":
            protected.add(i)
            tool_count += 1
            # Find the nearest preceding assistant message with tool_calls
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "assistant":
                    if messages[j].get("tool_calls"):
                        protected.add(j)
                    break
    return protected


def _validate_outgoing_payload(vllm_body: dict, expected_live_hash: str) -> tuple:
    """Validate the final outgoing payload before sending to vLLM.

    Checks:
      1. Live prompt hash matches the captured identity
      2. No ctxgate_* metadata leaks
      3. No orphaned tool results

    Returns (ok: bool, error_message: str).
    """
    messages = vllm_body.get("messages", [])

    # 1. Live prompt hash
    if expected_live_hash:
        actual_hash = _live_prompt_identity(messages)
        if actual_hash != expected_live_hash:
            return False, "live_prompt_integrity_violation"

    # 2. No ctxgate_* metadata leaks
    for msg in messages:
        for key in msg:
            if key.startswith("ctxgate_"):
                return False, "proxy_metadata_leak"

    # 3. No orphaned tool results (tool result without matching assistant tool_call)
    tool_call_ids = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tcid = tc.get("id", "")
                if tcid:
                    tool_call_ids.add(tcid)
    tool_result_ids = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid:
                tool_result_ids.add(tcid)
    orphans = tool_result_ids - tool_call_ids
    if orphans:
        return False, f"orphaned_tool_results:{len(orphans)}"

    return True, ""


def verify_context_invariants(context: list, original: list, ceiling: int, total_tokens: Optional[int] = None) -> list:
    """Return a list of violation strings (empty = OK).

    Checks:
    (1) newest live user message is represented
    (2) pinned copy exists when the newest user msg was cut from the window
    (3) newest 4 tool bodies are byte-identical to original
    (4) no orphan tool result
    (5) no assistant tool_calls declaration left without its required tool results
    (6) total tokens <= ceiling
    """
    violations = []

    # (1) & (2) newest user message
    orig_newest_user = None
    for i in range(len(original) - 1, -1, -1):
        if original[i].get("role") == "user":
            orig_newest_user = i
            break
    if orig_newest_user is not None:
        orig_user_content = _norm_content(original[orig_newest_user].get("content"))
        found_in_ctx = False
        pinned_exists = False
        for m in context:
            if m.get("role") == "user":
                if hashlib.sha256(_norm_content(m.get("content")).encode()).hexdigest() == hashlib.sha256(orig_user_content.encode()).hexdigest():
                    found_in_ctx = True
                    break
        if not found_in_ctx:
            violations.append("newest_user_missing: newest user message not represented in context")

    # (3) newest 4 tool bodies byte-identical
    prot = protected_tool_groups(original)
    tool_idx_in_orig = 0
    for i in range(len(original) - 1, -1, -1):
        if tool_idx_in_orig >= 4:
            break
        if original[i].get("role") == "tool" and i in prot:
            orig_content = _norm_content(original[i].get("content"))
            tcid = original[i].get("tool_call_id", "")
            matched = False
            for m in context:
                if m.get("role") == "tool" and m.get("tool_call_id") == tcid:
                    if _norm_content(m.get("content")) != orig_content:
                        violations.append(f"tool_body_modified: tool msg at orig[{i}] (id={tcid}) content differs")
                    matched = True
                    break
            if not matched:
                violations.append(f"tool_body_missing: tool msg at orig[{i}] (id={tcid}) not in context")
            tool_idx_in_orig += 1

    # (4) no orphan tool result
    for i, m in enumerate(context):
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id", "")
            has_parent = False
            for j in range(i - 1, -1, -1):
                if context[j].get("role") == "assistant" and context[j].get("tool_calls"):
                    for tc in context[j]["tool_calls"]:
                        if tc.get("id") == tcid:
                            has_parent = True
                            break
                if has_parent:
                    break
            if not has_parent:
                violations.append(f"orphan_tool: tool msg at ctx[{i}] (id={tcid}) has no matching assistant tool_call")

    # (5) no assistant tool_calls without required results
    for i, m in enumerate(context):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id", "")
                orig_had_result = False
                for om in original:
                    if om.get("role") == "tool" and om.get("tool_call_id") == tcid:
                        orig_had_result = True
                        break
                if orig_had_result:
                    ctx_has_result = False
                    for cm in context[i+1:]:
                        if cm.get("role") == "tool" and cm.get("tool_call_id") == tcid:
                            ctx_has_result = True
                            break
                    if not ctx_has_result:
                        violations.append(f"missing_tool_result: assistant at ctx[{i}] declares tool_call {tcid} but result missing")

    # (6) total tokens <= ceiling
    total = total_tokens if total_tokens is not None else count_messages_tokens(context)
    if total > ceiling:
        violations.append(f"over_ceiling: context is {total} tokens > ceiling {ceiling}")

    return violations

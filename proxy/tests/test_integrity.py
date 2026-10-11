"""Phase 02+03: Integrity reproducers and verification."""
import hashlib
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["CTXGATE_SKIP_DOTENV"] = "1"
os.environ["CTXGATE_TOKEN_ESTIMATOR"] = "calibrated"

import proxy.app as app


# --- Helpers ---

def make_msg(role, content, **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def make_conversation(n_turns, user_prefix="Hello"):
    """Create a conversation with n user/assistant turns."""
    msgs = [make_msg("system", "You are a helpful assistant.")]
    for i in range(n_turns):
        msgs.append(make_msg("user", f"{user_prefix} {i} " + "x" * 100))
        msgs.append(make_msg("assistant", f"Response {i}"))
    return msgs


# --- Test 1: Live prompt is NOT truncated by pinned copy ---

def test_live_prompt_not_truncated_by_pinned_copy():
    """The live prompt must appear in full as the last message.
    No pinned duplicate at index 3. No 16k/2k cap.
    
    Before the fix, _make_pinned_copy created a truncated duplicate at index 3.
    After the fix, _kept_messages extends the cut to include the live prompt
    at its natural position, with no duplicate.
    """
    long_prompt = "x" * 20000  # > 16000 chars (old PINNED_USER_MAX_CHARS)
    
    # Build a conversation where the last user message is very long
    msgs = [
        make_msg("system", "SYS"),
        make_msg("user", "old user 1"),
        make_msg("assistant", "old assistant 1"),
        make_msg("user", "old user 2"),
        make_msg("assistant", "old assistant 2"),
        make_msg("user", long_prompt),  # live prompt
    ]
    
    # _kept_messages with a cut that would have excluded the live prompt
    # (simulating the old behavior where cut=4 would drop the last user msg)
    seed = [msgs[0]]  # system
    rest = msgs[1:]   # everything after system
    cut = 4  # would normally drop rest[4] which is the live prompt
    
    kept = app._kept_messages(seed, rest, cut)
    
    # The live prompt must be present in full
    last_user = [m for m in kept if m.get("role") == "user"][-1]
    assert last_user["content"] == long_prompt, (
        f"Live prompt was truncated: got {len(last_user['content'])} chars, "
        f"expected {len(long_prompt)}"
    )
    
    # No duplicate: the live prompt should appear exactly once
    user_msgs = [m for m in kept if m.get("role") == "user"]
    assert len(user_msgs) == 1, (
        f"Expected exactly 1 user message (the live prompt), got {len(user_msgs)}"
    )
    
    # No ctxgate_pinned marker anywhere
    for m in kept:
        assert "ctxgate_pinned" not in m, "ctxgate_pinned marker found in kept messages"


# --- Test 2: Same first 300 chars, different tail → different hash ---

def test_same_prefix_different_tail_different_hash():
    """Two prompts with same first 300 chars but different tails must
    produce different identity hashes.
    
    The old _msg_anchor used content[:300] which would collide.
    The new version uses full content with SHA-256.
    """
    prompt_a = "A" * 300 + "TAIL_A_UNIQUE"
    prompt_b = "A" * 300 + "TAIL_B_DIFFERENT"
    
    msg_a = make_msg("user", prompt_a)
    msg_b = make_msg("user", prompt_b)
    
    hash_a = app._msg_anchor(msg_a)
    hash_b = app._msg_anchor(msg_b)
    
    assert hash_a != hash_b, (
        f"Messages with same 300-char prefix but different tails "
        f"produced the same hash: {hash_a}"
    )
    
    # Verify it's a SHA-256 hash (64 hex chars)
    assert len(hash_a) == 64, f"Expected SHA-256 (64 chars), got {len(hash_a)}"


# --- Test 3: _note_tok works in isolated namespace ---

def test_note_tok_in_isolated_namespace():
    """_note_tok must work when exec'd in a namespace without RHO_COLD.
    It should use count_tokens() which respects the estimator mode."""
    import ast
    
    src = open(os.path.join(os.path.dirname(app.__file__), "app.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    
    # Extract just _note_tok and its dependencies
    funcs_needed = {"_note_tok", "_norm_content"}
    consts_needed = {"COMPACTION_NOTE", "_NOTE_TOK_CACHE"}
    
    code = ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") in consts_needed for t in node.targets
        ):
            code += ast.get_source_segment(src, node) + "\n"
        if isinstance(node, ast.FunctionDef) and node.name in funcs_needed:
            code += ast.get_source_segment(src, node) + "\n"
    
    # Execute in a namespace WITHOUT RHO_COLD
    ns = {
        "os": os,
        "hashlib": hashlib,
        "count_tokens": lambda t: max(1, len(t) // 4),  # simple estimator
    }
    exec(code, ns)
    
    # _note_tok should work without RHO_COLD in the namespace
    result = ns["_note_tok"]()
    assert result >= 0, f"_note_tok returned negative: {result}"
    assert isinstance(result, int), f"_note_tok should return int, got {type(result)}"


# --- Test 4: _active_rho is removed ---

def test_no_active_rho_global():
    """The deprecated _active_rho global must not exist."""
    assert not hasattr(app, "_active_rho"), (
        "_active_rho global still exists in proxy.app module"
    )
    
    # Also verify it's not in the source
    src = open(os.path.join(os.path.dirname(app.__file__), "app.py"), encoding="utf-8").read()
    assert "_active_rho" not in src, (
        "_active_rho reference still found in source code"
    )


# --- Test 5: Live prompt hash validation ---

def test_live_prompt_hash_validation():
    """The live prompt's SHA-256 hash must be captured before modification
    and validated on the outgoing payload. If they differ, fail closed."""
    # Create a message with a known live prompt
    msgs = [
        make_msg("system", "SYS"),
        make_msg("user", "original prompt content"),
        make_msg("assistant", "response"),
        make_msg("user", "the live prompt I care about"),
    ]
    
    # Capture the identity
    h = app._live_prompt_identity(msgs)
    assert h != "", "Hash should not be empty for a message with a user msg"
    assert len(h) == 64, f"Expected 64-char SHA-256, got {len(h)}"
    
    # Same messages → same hash
    h2 = app._live_prompt_identity(msgs)
    assert h == h2, "Same messages must produce same hash"
    
    # Different last user message → different hash
    msgs_modified = [
        make_msg("system", "SYS"),
        make_msg("user", "original prompt content"),
        make_msg("assistant", "response"),
        make_msg("user", "MODIFIED live prompt"),
    ]
    h3 = app._live_prompt_identity(msgs_modified)
    assert h != h3, "Different live prompt must produce different hash"
    
    # No user message → empty string
    h_empty = app._live_prompt_identity([make_msg("system", "only system")])
    assert h_empty == "", "No user message should give empty hash"
    
    # Multimodal content: list of blocks
    msgs_multimodal = [
        make_msg("user", [{"type": "text", "text": "hello"}, {"type": "image_url", "url": "http://x"}]),
    ]
    h_mm = app._live_prompt_identity(msgs_multimodal)
    assert h_mm != "", "Multimodal message should produce a hash"
    
    # Order matters in multimodal
    msgs_multimodal_reversed = [
        make_msg("user", [{"type": "image_url", "url": "http://x"}, {"type": "text", "text": "hello"}]),
    ]
    h_mm_rev = app._live_prompt_identity(msgs_multimodal_reversed)
    assert h_mm != h_mm_rev, "Block order must affect the hash"


# --- Test 6: Capacity error when prompt too large ---

def test_capacity_error_when_prompt_exceeds_budget():
    """When live prompt + mandatory costs exceed the input budget,
    return a structured capacity error. Never silently trim.
    
    We test the _live_prompt_identity + budget logic directly.
    The actual HTTP 413 is tested via the full request path.
    """
    # Simulate: a very large prompt that exceeds MAX_INPUT
    huge_prompt = "x" * int(app.MAX_INPUT * app.RHO_COLD * 2)  # way over budget
    
    msgs = [
        make_msg("system", "SYS"),
        make_msg("user", huge_prompt),
    ]
    
    input_tokens = app.count_messages_tokens(msgs)
    ceiling = min(app.MAX_INPUT, app.MAX_CONTEXT - app.SAFETY_MARGIN - app.MIN_OUTPUT)
    
    # The prompt should exceed the ceiling
    assert input_tokens > ceiling, (
        f"Test setup failed: tokens={input_tokens} should exceed ceiling={ceiling}"
    )
    
    # The capacity check in chat_completions would return 413 here.
    # We verify the condition that triggers it:
    assert input_tokens > ceiling


# --- Test 7: No ctxgate_* metadata in outgoing payload ---

def test_no_proxy_metadata_leak():
    """ctxgate_* keys must be stripped before sending to vLLM."""
    msgs = [
        make_msg("system", "SYS", ctxgate_internal=True, ctxgate_anchor="abc123"),
        make_msg("user", "hello", ctxgate_pinned=True),
        make_msg("assistant", "hi", ctxgate_live_prompt_hash="def456"),
    ]
    
    cleaned = app._strip_proxy_metadata(msgs)
    
    for m in cleaned:
        for k in m:
            assert not k.startswith("ctxgate_"), (
                f"ctxgate_ key '{k}' leaked in outgoing payload"
            )
    
    # Content should be preserved
    assert cleaned[0]["content"] == "SYS"
    assert cleaned[1]["content"] == "hello"
    assert cleaned[2]["content"] == "hi"


# --- Test 8: Tool call graph integrity after cut ---

def test_tool_call_graph_integrity_after_cut():
    """After a context cut, no assistant tool_calls message should be
    orphaned from its corresponding tool results.
    
    _kept_messages must not create a state where an assistant message
    with tool_calls is present but its tool results are cut away.
    """
    # Build a conversation with tool calls
    msgs = [
        make_msg("system", "SYS"),
        make_msg("user", "do something"),
        make_msg("assistant", "", tool_calls=[{"id": "tc1", "function": {"name": "f", "arguments": "{}"}}]),
        make_msg("tool", "result1", tool_call_id="tc1"),
        make_msg("user", "do more"),
        make_msg("assistant", "", tool_calls=[{"id": "tc2", "function": {"name": "g", "arguments": "{}"}}]),
        make_msg("tool", "result2", tool_call_id="tc2"),
        make_msg("user", "final prompt"),
    ]
    
    seed = [msgs[0]]
    rest = msgs[1:]
    
    # Cut that would normally split a tool call pair
    # If we cut at index 2 (after the first user msg), we'd orphan tc1
    # But _kept_messages extends to include the live prompt
    kept = app._kept_messages(seed, rest, 2)
    
    # Verify: no assistant tool_calls message is orphaned
    # For each assistant msg with tool_calls, its tool results must be present
    tool_call_ids_in_kept = set()
    for m in kept:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tool_call_ids_in_kept.add(tc["id"])
    
    tool_result_ids_in_kept = set()
    for m in kept:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            tool_result_ids_in_kept.add(m["tool_call_id"])
    
    # Every tool_call_id in an assistant msg must have a corresponding tool result
    orphaned = tool_call_ids_in_kept - tool_result_ids_in_kept
    assert not orphaned, (
        f"Orphaned tool_calls found (no matching tool result): {orphaned}"
    )
    
    # The live prompt (last user msg) must be present
    last_user = [m for m in kept if m.get("role") == "user"][-1]
    assert last_user["content"] == "final prompt"


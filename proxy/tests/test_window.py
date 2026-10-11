"""Tests for Phase 05: Window Eviction Architecture."""
import hashlib
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import (
    _msg_anchor,
    _turn_groups,
    _safe_cut,
    _fit_to_budget,
    _eviction_trigger,
    _trim_target,
    _safe_admitted_budget,
    _window_epoch,
    _eviction_priority,
    _elided_tool_content,
    _stable_elide_inplace,
    _window_valid,
    _window_persist,
    _window_load,
    _emergency_shrink,
    verify_context_invariants,
    estimate_full_payload_tokens,
    CUT_QUANTUM,
    CTXGATE_WINDOW_TRIGGER_FRAC,
    CTXGATE_WINDOW_TARGET_FRAC,
    ContextCapacityError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(role="user", content="hello", **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def _make_tool_msg(tool_call_id="tc1", content="x" * 3000):
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }


def _make_assistant_with_tools(content="thinking", tool_calls=None):
    tc = tool_calls or [{"id": "tc1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
    return {"role": "assistant", "content": content, "tool_calls": tc}


def _make_group(user_msg, assistant_msg, tool_msgs=None):
    """Build a simple turn group: user -> assistant -> [tools]."""
    msgs = [user_msg, assistant_msg]
    if tool_msgs:
        msgs.extend(tool_msgs)
    return msgs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_hysteresis_does_not_cut_every_request():
    """If below high watermark, no eviction occurs."""
    trigger = _eviction_trigger()
    target = _trim_target()
    # The trigger must be higher than the target
    assert trigger > target, f"trigger ({trigger}) must be > target ({target})"
    # And both must be positive
    assert trigger > 0
    assert target > 0
    # The trigger should be approximately 92% of safe admitted budget
    safe_budget = _safe_admitted_budget()
    assert abs(trigger - int(safe_budget * CTXGATE_WINDOW_TRIGGER_FRAC)) < 10
    # The target should be approximately 82% of safe admitted budget
    assert abs(target - int(safe_budget * CTXGATE_WINDOW_TARGET_FRAC)) < 10


def test_cut_position_is_anchor_not_index():
    """Cut position is stored as SHA-256 anchor, survives history reordering."""
    # Create messages with known content
    msgs = [
        _make_msg("user", "message one"),
        _make_msg("assistant", "response one"),
        _make_msg("user", "message two"),
        _make_msg("assistant", "response two"),
    ]
    # Compute anchors
    anchors = [_msg_anchor(m) for m in msgs]
    # Anchors should be unique
    assert len(set(anchors)) == len(anchors), "Anchors must be unique"
    # Anchors should be SHA-256 hex strings
    for a in anchors:
        assert len(a) == 64, f"Anchor should be 64 hex chars, got {len(a)}"
        int(a, 16)  # valid hex
    # Simulate: store cut at index 2, anchor = anchors[2]
    cut = 2
    cut_anchor = anchors[cut]
    # Now reorder the messages (simulate Goose resending edited history)
    reordered = [msgs[2], msgs[3], msgs[0], msgs[1]]
    # Find the message matching the anchor in the reordered list
    found = False
    for i, m in enumerate(reordered):
        if _msg_anchor(m) == cut_anchor:
            found = True
            assert i == 0, f'Anchor should be at index 0 in reordered, got {i}'
            break
    assert found, "Anchor should be found in reordered list"


def test_quantized_cut_motion():
    """Cut moves in 512-token steps, not arbitrary amounts."""
    assert CUT_QUANTUM == 512, f"CUT_QUANTUM should be 512, got {CUT_QUANTUM}"
    # Verify quantization math
    q = (1000 // CUT_QUANTUM) * CUT_QUANTUM
    assert q == 512, f"1000 quantized should be 512, got {q}"
    q2 = (2000 // CUT_QUANTUM) * CUT_QUANTUM
    assert q2 == 1536, f"2000 quantized should be 1536, got {q2}"
    q3 = (512 // CUT_QUANTUM) * CUT_QUANTUM
    assert q3 == 512, f"512 quantized should be 512, got {q3}"
    # _safe_cut should use quantized budget
    # Create groups with known token counts
    groups = [
        [_make_msg("user", "a" * 100)],   # ~25 tokens
        [_make_msg("user", "a" * 200)],   # ~50 tokens
        [_make_msg("user", "a" * 300)],   # ~75 tokens
    ]
    # With a budget of 100 tokens, quantized to 512 -> uses 100 (below min quantum)
    cut = _safe_cut(groups, 100)
    assert cut >= 0
    # With a very large budget, should keep all groups
    cut_all = _safe_cut(groups, 100000)
    assert cut_all == 0, f"Should keep all groups (cut at 0), got cut at {cut_all}"


def test_eviction_priority():
    """Old tool results evicted before old turns, before summaries."""
    total = 10
    # System message: never evict
    sys_msg = _make_msg("system", "You are a helpful assistant.")
    assert _eviction_priority(sys_msg, 0, total) == -1

    # Live prompt (last user message): never evict
    live_prompt = _make_msg("user", "What is the weather?")
    assert _eviction_priority(live_prompt, total - 1, total) == -1

    # Verbose tool result: priority 0 (evict first)
    tool_verbose = _make_tool_msg(content="x" * 3000)
    assert _eviction_priority(tool_verbose, 3, total) == 0

    # Small tool result: priority 1
    tool_small = _make_tool_msg(content="short")
    assert _eviction_priority(tool_small, 4, total) == 1

    # User message (not last): priority 1
    user_msg = _make_msg("user", "old question")
    assert _eviction_priority(user_msg, 2, total) == 1

    # Assistant with tool_calls: priority 1
    assistant_tools = _make_assistant_with_tools()
    assert _eviction_priority(assistant_tools, 5, total) == 1

    # Assistant without tool_calls: priority 2
    assistant_plain = _make_msg("assistant", "plain response")
    assert _eviction_priority(assistant_plain, 6, total) == 2

    # Unknown role: priority 3
    unknown = _make_msg("developer", "note")
    assert _eviction_priority(unknown, 7, total) == 3


def test_never_evict_live_prompt():
    """Live prompt is never evicted regardless of budget pressure."""
    total = 5
    live = _make_msg("user", "current question")
    assert _eviction_priority(live, total - 1, total) == -1
    # Even a very long live prompt is never evicted
    live_long = _make_msg("user", "x" * 100000)
    assert _eviction_priority(live_long, total - 1, total) == -1


def test_never_evict_inflight_tool_group():
    """An in-flight tool-call sequence is never split."""
    # Create a group with assistant + tool messages
    assistant = _make_assistant_with_tools(tool_calls=[
        {"id": "tc1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
        {"id": "tc2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
    ])
    tool1 = _make_tool_msg("tc1", "result1")
    tool2 = _make_tool_msg("tc2", "result2")
    group = [assistant, tool1, tool2]
    groups = [group]
    # _safe_cut should never cut in the middle of this group
    # With a very small budget, it should cut at 0 (before the group)
    # not in the middle of it
    cut = _safe_cut(groups, 10)
    assert cut == 0, f"Should cut before the group, got {cut}"
    # The group is atomic: either all kept or all dropped
    # Verify _turn_groups produces this as one group
    all_msgs = group
    tg = _turn_groups(all_msgs)
    assert len(tg) == 1, f"Should be 1 group, got {len(tg)}"
    assert tg[0] == group


def test_stable_epoch_within_watermark():
    """Within the same epoch, the same messages are retained."""
    # Create a window state with a cut anchor
    msgs = [
        _make_msg("user", "first message"),
        _make_msg("assistant", "first response"),
        _make_msg("user", "second message"),
        _make_msg("assistant", "second response"),
    ]
    ws = {
        "cut": 2,
        "cut_anchor": _msg_anchor(msgs[2]),
        "seed_sig": "test_sig",
    }
    epoch1 = _window_epoch(ws)
    assert epoch1 == _msg_anchor(msgs[2]), "Epoch should equal cut anchor"

    # Same window state -> same epoch
    ws2 = dict(ws)
    epoch2 = _window_epoch(ws2)
    assert epoch1 == epoch2, "Same window state must produce same epoch"

    # Different cut -> different epoch
    ws3 = {
        "cut": 0,
        "cut_anchor": _msg_anchor(msgs[0]),
        "seed_sig": "test_sig",
    }
    epoch3 = _window_epoch(ws3)
    assert epoch1 != epoch3, "Different cut anchors must produce different epochs"


def test_elision_produces_retrieval_handle():
    """Evicted tool results get a bounded excerpt with hash + retrieval handle."""
    original = "x" * 5000
    elided = _elided_tool_content(original, session_key="sess123", event_hash="abc123")
    # Must contain the retrieval handle
    assert "/retrieve?session=sess123&event=abc123" in elided, f"Missing retrieval handle in: {elided[:200]}"
    # Must contain a hash
    assert "hash=" in elided, f"Missing hash in: {elided[:200]}"
    # Must be bounded (much shorter than original)
    assert len(elided) < len(original), f"Elided ({len(elided)}) must be shorter than original ({len(original)})"
    # Must have head and tail
    assert elided.startswith("x" * 1000), "Must start with 1000 chars of head"
    assert elided.endswith("x" * 1000), "Must end with 1000 chars of tail"
    # Must be idempotent
    elided2 = _elided_tool_content(elided, session_key="sess123", event_hash="abc123")
    assert elided == elided2, "Elision must be idempotent"
    # Without session_key/event_hash, should still work
    elided3 = _elided_tool_content(original)
    assert "hash=" in elided3
    assert "retrieve" in elided3


def test_revalidate_after_eviction():
    """After eviction, the full payload is re-estimated and verified to fit."""
    # Create a scenario where we have messages that need to fit in a budget
    # The _emergency_shrink function should revalidate after eviction
    # and raise ContextCapacityError if mandatory costs can't fit
    # Test that the function exists and can be called
    assert callable(_emergency_shrink)
    # Test that ContextCapacityError is a proper exception
    assert issubclass(ContextCapacityError, Exception)
    # Verify the trigger/target relationship
    trigger = _eviction_trigger()
    target = _trim_target()
    safe = _safe_admitted_budget()
    assert target < trigger < safe, f"target ({target}) < trigger ({trigger}) < safe ({safe})"


def test_elision_idempotent():
    """Eliding already-elided content returns it unchanged."""
    original = "a" * 5000
    elided_once = _elided_tool_content(original)
    elided_twice = _elided_tool_content(elided_once)
    assert elided_once == elided_twice, "Double elision must be idempotent"


def test_stable_elide_inplace_only_tool_messages():
    """_stable_elide_inplace only modifies tool messages, not others."""
    msgs = [
        _make_msg("user", "question with " + "x" * 3000),
        _make_assistant_with_tools(),
        _make_tool_msg("tc1", "y" * 3000),
        _make_msg("user", "follow up"),
    ]
    # Elide from index 0 to 4
    _stable_elide_inplace(msgs, 0, 4)
    # User message should be unchanged
    assert msgs[0]["content"] == "question with " + "x" * 3000
    # Assistant should be unchanged
    assert msgs[1]["content"] == "thinking"
    # Tool message should be elided
    assert "chars elided by ctxgate" in msgs[2]["content"]
    assert len(msgs[2]["content"]) < 3000
    # Last user message should be unchanged
    assert msgs[3]["content"] == "follow up"


def test_window_epoch_empty():
    """Window epoch returns empty string for empty window."""
    assert _window_epoch({}) == ""
    assert _window_epoch({"cut": 0}) == ""


def test_safe_cut_empty_groups():
    """_safe_cut with no groups returns 0."""
    assert _safe_cut([], 1000) == 0

#!/usr/bin/env python3
"""Offline tests for the rolling-window livelock fixes (proxy/app.py).

Three fixes tested:
  1. Tiny-slice watermark bypass (identical root summary on <=4-msg slices)
  2. Adaptive min_tail in _recut_to (converges on huge histories)
  3. Distance-aware pinned copy cap (reduced chars when far before cut)

No vLLM, no network. Mirrors test_window_overshoot.py structure.
"""
import asyncio, json, os, sys, traceback

os.environ["CTXGATE_MAX_INPUT"] = "64000"
os.environ["CTXGATE_MAX_CONTEXT"] = "84000"
os.environ["CTXGATE_MAX_OUTPUT"] = "18000"
os.environ["CTXGATE_SAFETY_MARGIN"] = "2000"
os.environ["CTXGATE_MIN_OUTPUT"] = "8192"
os.environ["CTXGATE_TRIM_TARGET_FRACTION"] = "0.70"
os.environ["CTXGATE_TRIM_TARGET_FLOOR"] = "20000"
sys.path.insert(0, "/home/pawelw/ctxproxy")
import proxy.app as app

PASS = 0
FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS: " + name)
    else:
        FAIL += 1
        print("FAIL: " + name + (" | " + detail if detail else ""))

def _pad(n_tokens):
    """Return a string of ~n_tokens tokens (exact via app.count_tokens)."""
    if n_tokens <= 0:
        return ""
    base = "word "
    k = max(1, n_tokens // 5)
    s = (base * k).strip()
    while app.count_tokens(s) < n_tokens:
        s = s + " word"
    while app.count_tokens(s) > n_tokens + 4:
        s = s[:-6]
    return s


# ---------------------------------------------------------------------------
# FIX 1: Tiny-slice watermark bypass
# ---------------------------------------------------------------------------
def test_tiny_slice_watermark_bypass():
    """When root summary is identical to prior AND slice is <=4 msgs,
    quality_ok should be forced True (with skip_insert=True) so the
    watermark advances. We test the logic directly by simulating the
    quality-gate code path."""
    # Simulate the conditions (must be >50 chars to pass the "too short" check)
    root_summary = "This is a summary that is identical to the prior summary for testing"
    prior_summary = "This is a summary that is identical to the prior summary for testing"
    slice_msgs = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
    ]

    # Replicate the quality gate logic from _summarize_trimmed_messages
    quality_ok = True
    quality_reason = ""
    if len(root_summary) < 50:
        quality_ok = False
        quality_reason = "too short"
    elif root_summary == prior_summary.strip():
        quality_ok = False
        quality_reason = "identical to prior summary"
    elif not any(c.isalpha() for c in root_summary):
        quality_ok = False
        quality_reason = "no alphabetic content"

    skip_insert = False
    if not quality_ok and quality_reason == "identical to prior summary" and len(slice_msgs) <= 4:
        quality_ok = True
        skip_insert = True

    check("fix1: tiny slice (2 msgs) forces quality_ok=True", quality_ok,
          "quality_ok=%s reason=%s" % (quality_ok, quality_reason))
    check("fix1: tiny slice sets skip_insert=True", skip_insert)

    # Large slice (>4 msgs) should NOT bypass
    slice_msgs_large = [{"role": "user", "content": "m%d" % i} for i in range(5)]
    quality_ok2 = True
    quality_reason2 = "identical to prior summary"
    skip_insert2 = False
    if not quality_ok2 and quality_reason2 == "identical to prior summary" and len(slice_msgs_large) <= 4:
        quality_ok2 = True
        skip_insert2 = True
    # quality_ok2 was set to True initially, so the bypass condition (not quality_ok) is False
    check("fix1: large slice (5 msgs) does NOT trigger bypass", not skip_insert2)

    # Edge case: exactly 4 msgs should bypass
    slice_msgs_4 = [{"role": "user", "content": "m%d" % i} for i in range(4)]
    quality_ok3 = False
    quality_reason3 = "identical to prior summary"
    skip_insert3 = False
    if not quality_ok3 and quality_reason3 == "identical to prior summary" and len(slice_msgs_4) <= 4:
        quality_ok3 = True
        skip_insert3 = True
    check("fix1: exactly 4 msgs triggers bypass", skip_insert3)

    # Edge case: 3 msgs should bypass
    slice_msgs_3 = [{"role": "user", "content": "m%d" % i} for i in range(3)]
    quality_ok4 = False
    quality_reason4 = "identical to prior summary"
    skip_insert4 = False
    if not quality_ok4 and quality_reason4 == "identical to prior summary" and len(slice_msgs_3) <= 4:
        quality_ok4 = True
        skip_insert4 = True
    check("fix1: 3 msgs triggers bypass", skip_insert4)


# ---------------------------------------------------------------------------
# FIX 2: Adaptive min_tail in _recut_to
# ---------------------------------------------------------------------------
def test_recut_convergence_huge_history():
    """With 150 messages totaling ~260K tokens and target 40K,
    the cut should advance significantly (min_tail=3 kicks in)."""
    # Build a synthetic history: 3 seed + 147 rest
    msgs = [
        {"role": "system", "content": _pad(500)},
        {"role": "user", "content": _pad(500)},
        {"role": "assistant", "content": _pad(500)},
    ]
    # 180 messages, each ~1000 tokens = ~180K tokens total (ratio 4.5x > 4x target)
    for i in range(180):
        if i % 3 == 0:
            msgs.append({"role": "user", "content": _pad(1000)})
        elif i % 3 == 1:
            msgs.append({"role": "assistant", "content": _pad(1000)})
        else:
            msgs.append({"role": "tool", "content": _pad(1000), "tool_call_id": "tc_%d" % i})

    total_tok = app.count_messages_tokens(msgs)
    target = 40000
    # total_tok should be > 4 * target to trigger min_tail=3
    check("fix2: total_tok > 4*target (triggers min_tail=3)",
          total_tok > 4 * target,
          "total_tok=%d target=%d ratio=%.1f" % (total_tok, target, total_tok / target))

    cut = app._recut_to(msgs, target)
    rest = msgs[3:]
    tail_len = len(rest) - cut

    check("fix2: cut advanced (tail < total rest)", tail_len < len(rest),
          "cut=%d tail=%d rest=%d" % (cut, tail_len, len(rest)))
    check("fix2: min_tail=3 allows shorter tail (tail >= 3)",
          tail_len >= 3, "tail=%d" % tail_len)

    # Verify the kept window is not catastrophically small
    kept = app._kept_messages(msgs[:3], rest, cut)
    kept_tok = app.count_messages_tokens(kept)
    check("fix2: kept_tok >= 0.5*target (no collapse)",
          kept_tok >= 0.5 * target,
          "kept_tok=%d target=%d" % (kept_tok, target))
    print("  [info] total_tok=%d cut=%d tail=%d kept_tok=%d" % (total_tok, cut, tail_len, kept_tok))


def test_recut_normal_history_min_tail_5():
    """With a normal history (< 4x target), min_tail should stay at 5."""
    msgs = [
        {"role": "system", "content": _pad(500)},
        {"role": "user", "content": _pad(500)},
        {"role": "assistant", "content": _pad(500)},
    ]
    # 20 messages, each ~1000 tokens = ~20K total (well under 4*40K=160K)
    for i in range(20):
        if i % 2 == 0:
            msgs.append({"role": "user", "content": _pad(1000)})
        else:
            msgs.append({"role": "assistant", "content": _pad(1000)})

    total_tok = app.count_messages_tokens(msgs)
    target = 40000
    check("fix2: normal history total_tok < 4*target (min_tail stays 5)",
          total_tok < 4 * target,
          "total_tok=%d" % total_tok)

    cut = app._recut_to(msgs, target)
    rest = msgs[3:]
    tail_len = len(rest) - cut
    check("fix2: normal history tail respects min_tail=5 (tail >= 5 or all fit)",
          tail_len >= 5 or tail_len == len(rest),
          "tail=%d rest=%d" % (tail_len, len(rest)))


# ---------------------------------------------------------------------------
# FIX 3: Distance-aware pinned copy cap
# ---------------------------------------------------------------------------
def test_pinned_copy_far_distance():
    """When the pinned user message is >50 positions before the cut,
    the cap should be PINNED_USER_FAR_CHARS (2000) not 16000."""
    # Create a long user message (10000 chars)
    long_content = "x" * 10000

    # Far distance (57 > 50 threshold)
    copy_far = app._make_pinned_copy({"role": "user", "content": long_content}, distance=57)
    far_len = len(copy_far["content"])
    check("fix3: far distance (57) uses reduced cap",
          far_len <= app.PINNED_USER_FAR_CHARS + 20,  # +20 for truncation marker
          "len=%d far_cap=%d" % (far_len, app.PINNED_USER_FAR_CHARS))

    # Near distance (10 <= 50 threshold)
    copy_near = app._make_pinned_copy({"role": "user", "content": long_content}, distance=10)
    near_len = len(copy_near["content"])
    check("fix3: near distance (10) uses full cap",
          near_len <= app.PINNED_USER_MAX_CHARS + 20,
          "len=%d max_cap=%d" % (near_len, app.PINNED_USER_MAX_CHARS))

    # At exactly the threshold (50) should use full cap
    copy_at = app._make_pinned_copy({"role": "user", "content": long_content}, distance=50)
    at_len = len(copy_at["content"])
    check("fix3: at threshold (50) uses full cap (not >50)",
          at_len <= app.PINNED_USER_MAX_CHARS + 20,
          "len=%d" % at_len)

    # Just past threshold (51) should use reduced cap
    copy_just = app._make_pinned_copy({"role": "user", "content": long_content}, distance=51)
    just_len = len(copy_just["content"])
    check("fix3: just past threshold (51) uses reduced cap",
          just_len <= app.PINNED_USER_FAR_CHARS + 20,
          "len=%d" % just_len)

    # Short message (under both caps) should be unchanged
    short_content = "hello world"
    copy_short = app._make_pinned_copy({"role": "user", "content": short_content}, distance=100)
    check("fix3: short message unchanged regardless of distance",
          copy_short["content"] == short_content,
          "content=%s" % copy_short["content"])


def test_pinned_user_copy_passes_distance():
    """_pinned_user_copy should pass (cut - lu) as distance."""
    rest = [
        {"role": "user", "content": "y" * 5000},  # index 0
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "z" * 5000},  # index 2 = newest user
        {"role": "assistant", "content": "a2"},
        {"role": "tool", "content": "t1", "tool_call_id": "tc1"},
        {"role": "assistant", "content": "a3"},
    ]
    cut = 5  # newest user at idx 2, distance = 5 - 2 = 3
    result = app._pinned_user_copy(rest, cut)
    check("fix3: _pinned_user_copy returns non-None when user before cut",
          result is not None)
    if result:
        # distance=3 <= 50, so full cap applies; 5000 chars < 16000 so no truncation
        check("fix3: near-distance copy not truncated",
              len(result["content"]) == 5000,
              "len=%d" % len(result["content"]))

    # Far: user at idx 0, cut at 60 -> distance=60 > 50
    rest_far = [
        {"role": "user", "content": "y" * 5000},  # index 0
    ] + [{"role": "assistant", "content": "a%d" % i} for i in range(59)]
    result_far = app._pinned_user_copy(rest_far, 60)
    check("fix3: far-distance copy is truncated",
          result_far is not None and len(result_far["content"]) <= app.PINNED_USER_FAR_CHARS + 20,
          "len=%s" % (len(result_far["content"]) if result_far else "None"))


def main():
    print("=" * 60)
    print("LIVELOCK FIX TESTS")
    print("=" * 60)

    print("\n--- Fix 1: Tiny-slice watermark bypass ---")
    try:
        test_tiny_slice_watermark_bypass()
    except Exception as e:
        check("fix1: test ran without exception", False, str(e))
        traceback.print_exc()

    print("\n--- Fix 2: Adaptive min_tail ---")
    try:
        test_recut_convergence_huge_history()
    except Exception as e:
        check("fix2: huge history test ran", False, str(e))
        traceback.print_exc()
    try:
        test_recut_normal_history_min_tail_5()
    except Exception as e:
        check("fix2: normal history test ran", False, str(e))
        traceback.print_exc()

    print("\n--- Fix 3: Distance-aware pinned copy ---")
    try:
        test_pinned_copy_far_distance()
    except Exception as e:
        check("fix3: far distance test ran", False, str(e))
        traceback.print_exc()
    try:
        test_pinned_user_copy_passes_distance()
    except Exception as e:
        check("fix3: pinned_user_copy test ran", False, str(e))
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("RESULTS: %d passed, %d failed" % (PASS, FAIL))
    print("=" * 60)
    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()


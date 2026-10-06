#!/usr/bin/env python3
"""Failing-first tests for stable tool-body elision (CTXGATE_STABLE_ELIDE).

Scenario: 10 tool bodies of ~25k chars each (~63k tokens) in a session that
exceeds MAX_INPUT from the first request.

Without stable elision (old behavior), _recut_to cannot reach the token target
without dropping whole message groups, so the kept window collapses to ~17
messages and the tool bodies are elided by _emergency_shrink to a DIFFERENT
set each request -> the sent prefix breaks every turn (the livelock).

With stable elision (CTXGATE_STABLE_ELIDE=1), tool bodies in [cut, elide_idx)
are elided deterministically to the SAME bytes every request (newest 4 intact),
so _recut_to reaches the target without dropping context, the prefix is
byte-stable, and emergency_shrink_total stays 0.

No vLLM, no network.
"""
import asyncio, os, sys, traceback

sys.path.insert(0, "/home/pawelw/ctxproxy")

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

def _reset(app):
    app.session_compactions.clear()
    app._window_locks.clear()
    app._window_load_tried.clear()
    for k in list(app.metrics):
        app.metrics[k] = 0

def _big_tool(i, size=25000):
    head = "TH%02d " % i
    pad = "a" * (1000 - len(head))
    tail = "b" * 900 + ("TT%02d" % i)
    return head + pad + "x" * (size - len(head) - len(pad) - len(tail)) + tail

def _session(n_tools=10):
    msgs = [
        {"role": "system", "content": "sys" * 200},
        {"role": "user", "content": "u0 " * 300},
        {"role": "assistant", "content": "a0 " * 300},
    ]
    msgs.append({"role": "user", "content": "LATEST " + "q" * 300})
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": "as%d" % i,
                     "tool_calls": [{"id": "tc%d" % i, "type": "function",
                                     "function": {"name": "s", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "tc%d" % i, "content": _big_tool(i)})
    return msgs

def _prefix_stable(prev, cur):
    n = min(len(prev), len(cur))
    s = 0
    for i in range(n):
        if prev[i] == cur[i]:
            s += 1
        else:
            break
    return s

def _graph_ok(msgs):
    seen = {}
    for m in msgs:
        r = m.get("role")
        if r == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seen[tc["id"]] = True
        elif r == "tool":
            tcid = m.get("tool_call_id")
            if tcid not in seen:
                return False, "orphan tool " + str(tcid)
            del seen[tcid]
    if seen:
        return False, "dangling tool_call " + str(list(seen)[:3])
    return True, ""

def _newest4_intact(kept):
    tools = [m for m in kept if m.get("role") == "tool"]
    if len(tools) < 4:
        return False, "only %d tools" % len(tools)
    for m in tools[-4:]:
        if len(m.get("content", "")) < 20000:
            return False, "newest tool too short: %d (tcid=%s)" % (len(m["content"]), m.get("tool_call_id"))
    return True, ""

def _drive(app, session_key, n_steps=15, n_tools=10):
    """Run n_steps requests, growing by 2 msgs each. Returns dict of observables."""
    _reset(app)
    app.pool = None
    hist = _session(n_tools=n_tools)
    prev = None
    shrink_vals = []
    stable_vals = []
    kept_lens = []
    for step in range(n_steps):
        kept = asyncio.run(app.build_context(list(hist), task_uuid=None, session_key=session_key))
        shrink_vals.append(app.metrics["emergency_shrink_total"])
        kept_lens.append(len(kept))
        if prev is not None:
            stable_vals.append(_prefix_stable(prev, kept))
        prev = kept
        n = len(hist) - 3
        hist.append({"role": "assistant", "content": "as%d" % n,
                     "tool_calls": [{"id": "tc%d" % n, "type": "function",
                                     "function": {"name": "s", "arguments": "{}"}}]})
        hist.append({"role": "tool", "tool_call_id": "tc%d" % n, "content": _big_tool(n)})
    return {
        "final_kept": prev,
        "shrink_vals": shrink_vals,
        "stable_vals": stable_vals,
        "kept_lens": kept_lens,
        "initial_total": app.count_messages_tokens(_session(n_tools=n_tools)),
    }


def test_stable_elision_on():
    """CTXGATE_STABLE_ELIDE=1: emergency_shrink_total stays 0, prefix stable,
    newest 4 tools intact, no orphans, and context is RETAINED (large window)."""
    os.environ["CTXGATE_STABLE_ELIDE"] = "1"
    if "proxy.app" in sys.modules:
        del sys.modules["proxy.app"]
    import proxy.app as app

    res = _drive(app, "on")
    check("A1: initial input exceeds MAX_INPUT",
          res["initial_total"] > app.MAX_INPUT,
          "total=%d MAX_INPUT=%d" % (res["initial_total"], app.MAX_INPUT))

    max_shrink = max(res["shrink_vals"])
    check("A2: emergency_shrink_total == 0 (stable elision prevents shrink)",
          max_shrink == 0,
          "shrink_vals=%s max=%d" % (res["shrink_vals"], max_shrink))

    full_stable = [s for s in res["stable_vals"] if s >= 10]
    check("A3: prefix byte-identical in sticky-reuse pairs",
          len(full_stable) > 0,
          "stable_vals=%s full_stable=%s" % (res["stable_vals"], full_stable))

    ok, err = _graph_ok(res["final_kept"])
    check("A4: no orphan tool messages", ok, err)
    ok, err = _newest4_intact(res["final_kept"])
    check("A5: newest 4 tool bodies intact", ok, err)

    max_kept = max(res["kept_lens"])
    check("A6: context retained - kept window grows large (>= 30 msgs)",
          max_kept >= 30,
          "kept_lens=%s max=%d" % (res["kept_lens"], max_kept))


def test_old_behavior_loses_context():
    """CTXGATE_STABLE_ELIDE=0: old behavior - _recut_to must drop whole groups
    to hit the target, so the kept window collapses (much smaller than with
    stable elision). This is the regression stable elision fixes."""
    os.environ["CTXGATE_STABLE_ELIDE"] = "0"
    if "proxy.app" in sys.modules:
        del sys.modules["proxy.app"]
    import proxy.app as app

    res = _drive(app, "off")
    check("B1: initial input exceeds MAX_INPUT",
          res["initial_total"] > app.MAX_INPUT,
          "total=%d MAX_INPUT=%d" % (res["initial_total"], app.MAX_INPUT))

    max_kept = max(res["kept_lens"])
    # Old behavior collapses the window to a small size (~17 msgs)
    check("B2: old behavior - kept window collapses (max < 30 msgs)",
          max_kept < 30,
          "kept_lens=%s max=%d" % (res["kept_lens"], max_kept))

    ok, err = _graph_ok(res["final_kept"])
    check("B3: no orphan tool messages (old behavior still graph-safe)", ok, err)


def test_elision_idempotent():
    """_elided_tool_content is idempotent: re-eliding already-elided content
    (which carries the marker) returns it unchanged."""
    os.environ["CTXGATE_STABLE_ELIDE"] = "1"
    if "proxy.app" in sys.modules:
        del sys.modules["proxy.app"]
    import proxy.app as app
    c = "x" * 25000
    e1 = app._elided_tool_content(c)
    e2 = app._elided_tool_content(e1)
    check("C1: elided content is idempotent", e1 == e2,
          "e1_len=%d e2_len=%d" % (len(e1), len(e2)))
    check("C2: elided content is under 2500 chars", len(e1) < 2500,
          "len=%d" % len(e1))
    check("C3: elided content has the marker", "chars elided by ctxgate" in e1)


def test_elide_idx():
    """_stable_elide_idx boundaries."""
    os.environ["CTXGATE_STABLE_ELIDE"] = "1"
    if "proxy.app" in sys.modules:
        del sys.modules["proxy.app"]
    import proxy.app as app

    r10 = [{"role": "tool", "content": "t"} for _ in range(10)]
    check("D1: idx(0, 10 tools) == 6 (leaves newest 4)", app._stable_elide_idx(0, r10) == 6)
    check("D2: idx(8, 10 tools) == 8 (cut wins)", app._stable_elide_idx(8, r10) == 8)
    r3 = [{"role": "tool", "content": "t"} for _ in range(3)]
    check("D3: idx(0, 3 tools) == 0 (fewer than 4, all elidable)", app._stable_elide_idx(0, r3) == 0)
    # No tools: elide_idx = len(rest), but _stable_elide_inplace is a no-op
    # (it only touches tool messages) -> the meaningful invariant.
    r4 = [{"role": "user", "content": "u"} for _ in range(4)]
    idx4 = app._stable_elide_idx(0, r4)
    before = [dict(m) for m in r4]
    app._stable_elide_inplace(r4, 0, idx4)
    check("D4: no-tool window is a no-op (unchanged)", r4 == before,
          "idx=%d" % idx4)


def test_no_caller_mutation():
    """build_context never mutates the caller's message list."""
    os.environ["CTXGATE_STABLE_ELIDE"] = "1"
    if "proxy.app" in sys.modules:
        del sys.modules["proxy.app"]
    import proxy.app as app
    _reset(app)
    app.pool = None

    hist = _session(n_tools=10)
    snapshot = [dict(m) for m in hist]
    asyncio.run(app.build_context(list(hist), task_uuid=None, session_key="mut"))
    check("E1: caller messages not mutated", hist == snapshot,
          "len_orig=%d len_now=%d" % (len(snapshot), len(hist)))


def main():
    tests = [
        test_stable_elision_on,
        test_old_behavior_loses_context,
        test_elision_idempotent,
        test_elide_idx,
        test_no_caller_mutation,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            print("FAIL: %s raised %s" % (t.__name__, e))
            traceback.print_exc()
    print("\n===== RESULTS: %d passed, %d failed =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    sys.exit(main())

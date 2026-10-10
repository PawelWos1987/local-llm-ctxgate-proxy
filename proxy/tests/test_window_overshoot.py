#!/usr/bin/env python3
"""Offline tests for the context-overshoot / output-starvation fix (proxy/app.py).

Scenario: a long agentic turn - 75 assistant(tool_calls)/tool pairs after the newest
user message - that the old "pin by keeping everything" RECUT kept at ~81k tokens
with MAX_INPUT=64000, starving the output budget (850 -> 638 -> 410 -> 192 -> negative).

Verifies the pinned-COPY + emergency-shrink + output-budget fix. No vLLM, no network.
Mirrors the structure of probes/rw/live/test_rolling_window.py.
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

def reset_state():
    app.session_compactions.clear()
    app._window_locks.clear()
    app._window_load_tried.clear()
    for k in ("trim_events", "trim_sticky_reuse", "dropped_total",
              "emergency_shrink_total", "emergency_shrink_groups_dropped",
              "recut_user_pinned"):
        app.metrics[k] = 0

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

def tool_graph_valid(msgs):
    seen_tc = {}
    for m in msgs:
        r = m.get("role")
        if r == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seen_tc[tc["id"]] = True
        elif r == "tool":
            tcid = m.get("tool_call_id")
            if tcid not in seen_tc:
                return False, "orphan tool result " + str(tcid)
            del seen_tc[tcid]
    if seen_tc:
        return False, "assistant tool_call without result: " + str(list(seen_tc)[:3])
    return True, ""

def make_overshoot_history(n_pairs=75, per_pair_tokens=1000):
    """3 seed + 1 old user + 1 newest user + n_pairs assistant/tool pairs (~1000 tok
    each). Total far exceeds MAX_INPUT so build_context must trim + pin."""
    msgs = [
        {"role": "system", "content": _pad(500)},
        {"role": "user", "content": _pad(500)},
        {"role": "assistant", "content": _pad(500)},
    ]
    msgs.append({"role": "user", "content": "OLD_USER_MESSAGE " + _pad(100)})
    msgs.append({"role": "user", "content": "UNIQUE_NEWEST_USER_MARKER " + _pad(400)})
    for i in range(n_pairs):
        msgs.append({"role": "assistant", "content": ("turn %d " % i) + _pad(per_pair_tokens // 2),
                     "tool_calls": [{"id": "tc%d" % i, "type": "function",
                                     "function": {"name": "shell", "arguments": json.dumps({"cmd": "ls %d" % i})}}]})
        msgs.append({"role": "tool", "tool_call_id": "tc%d" % i, "content": ("out %d " % i) + _pad(per_pair_tokens // 2)})
    return msgs

# ===================== TEST 1: overshoot -> slow path with pinned copy =====================
def test_1_overshoot_slow_path():
    reset_state()
    app.pool = None
    hist = make_overshoot_history()
    total = app.count_messages_tokens(hist)
    check("1a input exceeds MAX_INPUT (triggers trim)", total > app.MAX_INPUT, "total=%d" % total)
    async def _go():
        return await app.build_context(hist, task_uuid=None, session_key="os1")
    kept = asyncio.run(_go())
    ceiling = min(app.MAX_INPUT, app.MAX_CONTEXT - app.SAFETY_MARGIN - app.MIN_OUTPUT)
    kept_tok = app.count_messages_tokens(kept)
    check("1b kept tokens <= ceiling", kept_tok <= ceiling, "kept_tok=%d ceiling=%d" % (kept_tok, ceiling))
    headroom = ceiling - kept_tok
    check("1c headroom non-negative", headroom >= 0, "headroom=%d" % headroom)
    occ = sum(1 for m in kept if isinstance(m.get("content"), str) and "UNIQUE_NEWEST_USER_MARKER" in m["content"])
    check("1d newest user text present exactly once", occ == 1, "occ=%d" % occ)
    pin_idx = next((i for i, m in enumerate(kept) if isinstance(m.get("content"), str) and m["content"].startswith("UNIQUE_NEWEST_USER_MARKER")), -1)
    check("1e pinned copy present", pin_idx >= 0, "pin_idx=%d" % pin_idx)
    if pin_idx >= 0 and pin_idx + 1 < len(kept):
        nxt = kept[pin_idx + 1]
        check("1f first msg after pinned copy is not role=tool", nxt.get("role") != "tool", "role=%s" % nxt.get("role"))
    else:
        check("1f first msg after pinned copy is not role=tool", False, "no message after pinned copy")
    ok, err = tool_graph_valid(kept)
    check("1g every tool msg has earlier assistant tool_call", ok, err)
    raw_newest = hist[4]
    if pin_idx >= 0:
        check("1h pinned copy anchor matches raw newest user",
              app._msg_anchor(kept[pin_idx]) == app._msg_anchor(raw_newest),
              "copy=%s raw=%s" % (app._msg_anchor(kept[pin_idx]), app._msg_anchor(raw_newest)))
    else:
        check("1h pinned copy anchor matches raw newest user", False, "no pinned copy")

# ===================== TEST 2: sticky fast path on a second (growing) call =====================
def test_2_sticky_fast_path():
    reset_state()
    app.pool = None
    hist = make_overshoot_history()
    async def _go():
        k1 = await app.build_context(hist, task_uuid=None, session_key="os2")
        hist2 = list(hist)
        hist2.append({"role": "assistant", "content": "turn 999 " + _pad(500),
                      "tool_calls": [{"id": "tc999", "type": "function",
                                      "function": {"name": "shell", "arguments": json.dumps({"cmd": "ls 999"})}}]})
        hist2.append({"role": "tool", "tool_call_id": "tc999", "content": "out 999 " + _pad(500)})
        k2 = await app.build_context(hist2, task_uuid=None, session_key="os2")
        return k1, k2
    k1, k2 = asyncio.run(_go())
    check("2a sticky-reuse taken (trim_sticky_reuse >= 1)", app.metrics["trim_sticky_reuse"] >= 1,
          "metric=%d" % app.metrics["trim_sticky_reuse"])
    n = min(len(k1), len(k2))
    stable = 0
    for i in range(n):
        if k1[i] == k2[i]:
            stable += 1
        else:
            break
    check("2b prefix of first %d msgs byte-identical between calls" % n, stable == n and n > 0,
          "stable=%d/%d" % (stable, n))
    ceiling = min(app.MAX_INPUT, app.MAX_CONTEXT - app.SAFETY_MARGIN - app.MIN_OUTPUT)
    check("2c second-call kept tokens <= ceiling", app.count_messages_tokens(k2) <= ceiling,
          "kept_tok=%d ceiling=%d" % (app.count_messages_tokens(k2), ceiling))

# ===================== TEST 3: _output_budget floor =====================
def test_3_output_budget():
    reset_state()
    app.pool = None
    hist = make_overshoot_history()
    async def _go():
        return await app.build_context(hist, task_uuid=None, session_key="os3")
    kept = asyncio.run(_go())
    in_tok = app.count_messages_tokens(kept)
    budget = app._output_budget(in_tok)
    check("3a handler _output_budget >= MIN_OUTPUT", budget >= app.MIN_OUTPUT,
          "budget=%d min=%d input=%d" % (budget, app.MIN_OUTPUT, in_tok))
    check("3b _output_budget(0) non-negative and capped", 0 < app._output_budget(0) <= app.MAX_OUTPUT,
          "budget0=%d" % app._output_budget(0))
    check("3c _output_budget at ceiling >= MIN_OUTPUT", app._output_budget(app.MAX_INPUT) >= app.MIN_OUTPUT,
          "budget_at_ceiling=%d" % app._output_budget(app.MAX_INPUT))

# ===================== TEST 4: regression - ordinary 40k session untouched =====================
def test_4_regression_40k():
    reset_state()
    app.pool = None
    msgs = [
        {"role": "system", "content": _pad(500)},
        {"role": "user", "content": _pad(500)},
        {"role": "assistant", "content": _pad(500)},
    ]
    for i in range(39):
        msgs.append({"role": "assistant", "content": ("t%d " % i) + _pad(500),
                     "tool_calls": [{"id": "tc%d" % i, "type": "function",
                                     "function": {"name": "shell", "arguments": json.dumps({"cmd": "ls %d" % i})}}]})
        msgs.append({"role": "tool", "tool_call_id": "tc%d" % i, "content": ("o%d " % i) + _pad(500)})
    total = app.count_messages_tokens(msgs)
    check("4a 40k session under MAX_INPUT", total <= app.MAX_INPUT, "total=%d" % total)
    async def _go():
        return await app.build_context(msgs, task_uuid=None, session_key="os4")
    kept = asyncio.run(_go())
    check("4b returned untouched (same message count)", len(kept) == len(msgs),
          "in=%d out=%d" % (len(msgs), len(kept)))
    has_stub = any(app.COMPACTION_NOTE in (m.get("content") or "") for m in kept)
    check("4c no compaction note inserted", not has_stub)
    has_pin = sum(1 for m in kept if isinstance(m.get("content"), str) and m["content"].startswith("UNIQUE_NEWEST_USER_MARKER"))
    check("4d no pinned copy", has_pin == 0)

def main():
    tests = [test_1_overshoot_slow_path, test_2_sticky_fast_path, test_3_output_budget, test_4_regression_40k]
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

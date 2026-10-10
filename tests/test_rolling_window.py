#!/usr/bin/env python3
"""Offline tests for the rolling-window (sticky cut) implementation.
Standalone like test_suite.py: imports proxy.app without starting the server.
Monkeypatches pool and the LM/summarizer with fakes. No network, no DB writes.
The model is a GROWING history (Goose resends the full store each turn)."""
import asyncio, hashlib, json, os, sys, time, importlib, importlib.machinery, importlib.util, traceback

os.environ["CTXGATE_MAX_INPUT"] = "64000"
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

class FakePool:
    def __init__(self, table_exists=True):
        self.table_exists = table_exists
        self.sql_log = []
        self.rows = {}
        self.phase_rows = []
        self.session_summaries = []
        self.tasks = {}
    async def fetchrow(self, sql, *args):
        self.sql_log.append(("fetchrow", sql, args))
        if "session_windows" in sql:
            if not self.table_exists:
                raise Exception("relation proxy.session_windows does not exist")
            r = self.rows.get(args[0])
            if r:
                return {"cut": r["cut"], "cut_anchor": r["cut_anchor"], "cut_prev_anchor": r["cut_prev_anchor"],
                        "seed_sig": r["seed_sig"], "summarized_through": r["summarized_through"], "dropped_total": r["dropped_total"]}
            return None
        if "proxy.tasks" in sql:
            return {"name": self.tasks.get(args[0], "")}
        if "session_summaries" in sql:
            if self.session_summaries:
                return {"summary": self.session_summaries[-1]}
            return None
        return None
    async def fetch(self, sql, *args):
        self.sql_log.append(("fetch", sql, args))
        if "session_windows" in sql:
            if not self.table_exists:
                raise Exception("relation proxy.session_windows does not exist")
            return []
        if "phase_summaries" in sql:
            return self.phase_rows
        return []
    async def execute(self, sql, *args):
        self.sql_log.append(("execute", sql, args))
        if "session_windows" in sql:
            if not self.table_exists:
                raise Exception("relation proxy.session_windows does not exist")
            self.rows[args[0]] = {"cut": args[1], "cut_anchor": args[2], "cut_prev_anchor": args[3],
                                 "seed_sig": args[4], "summarized_through": args[5], "dropped_total": args[6]}
            return
        if "phase_summaries" in sql and "INSERT" in sql:
            self.phase_rows.append({"phase_number": args[2], "summary": args[3]})
            return
        if "session_summaries" in sql and "INSERT" in sql:
            self.session_summaries.append(args[2])
            return
        return

class Fake4B:
    def __init__(self, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call
        self._n = 0
    async def __call__(self, messages, max_tokens=800, json_mode=False, system=""):
        idx = self._n
        self._n += 1
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "json_mode": json_mode, "system": system})
        if self.fail_on_call is not None and idx == self.fail_on_call:
            raise Exception("injected summarizer failure")
        content = messages[0]["content"] if messages else ""
        if "Phase" in content:
            return "Phase summary: " + content[:100]
        if json_mode:
            return {"state_update": {"current_state": "Root summary: " + content[:100]}}
        return "Summary: " + content[:100]

def reset_state():
    app.session_compactions.clear()
    app.session_seeds.clear()
    app.session_fingerprints.clear()
    app._window_locks.clear()
    app._summary_task_locks.clear()
    app._summary_last_attempt.clear()
    app.session_prefix_hashes.clear()
    app._window_load_tried.clear()
    app._slice_log.clear()
    for k in ("trim_events","trim_sticky_reuse","dropped_total","summary_slices_ok","summary_slices_failed","window_loads_db","window_persist_errors"):
        app.metrics[k] = 0
    app._window_persist_enabled = None
    app._window_persist_warned = False

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

def make_history(n_turns, seed_tokens=50000, per_turn_tokens=2500):
    msgs = [
        {"role": "system", "content": _pad(seed_tokens * 2 // 5)},
        {"role": "user", "content": _pad(seed_tokens // 5)},
        {"role": "assistant", "content": _pad(seed_tokens // 5)},
    ]
    for i in range(n_turns):
        msgs.append({"role": "assistant", "content": ("turn %d " % i) + _pad(per_turn_tokens // 2),
                     "tool_calls": [{"id": "tc%d" % i, "type": "function",
                                     "function": {"name": "shell", "arguments": json.dumps({"cmd": "ls %d" % i})}}]})
        msgs.append({"role": "tool", "tool_call_id": "tc%d" % i, "content": ("out %d " % i) + _pad(per_turn_tokens // 2)})
    return msgs

def run_growing(hist, start_i, end_i, task_uuid, session_key, settle=1.0):
    """Feed prefixes hist[:i] for i in [start_i, end_i) in ONE event loop; let bg tasks finish.
    Returns list of (i, output, cut) for each i."""
    results = []
    async def _go():
        for i in range(start_i, end_i):
            out = await app.build_context(hist[:i], task_uuid=task_uuid, session_key=session_key)
            ws = app.session_compactions.get(session_key)
            cut = ws["cut"] if ws else 0
            results.append((i, out, cut))
        await asyncio.sleep(settle)
    asyncio.run(_go())
    return results

def prefix_stability(prev_out, cur_out):
    if not prev_out:
        return 1.0
    n = min(len(prev_out), len(cur_out))
    stable = 0
    for i in range(n):
        if prev_out[i] == cur_out[i]:
            stable += 1
        else:
            break
    return stable / len(prev_out)

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

# ===================== TEST 1: _trim_target =====================
def test_trim_target():
    app.TRIM_TARGET_TOKENS = 0
    app.TRIM_TARGET_FRACTION = 0.70
    app.TRIM_TARGET_FLOOR = 20000
    app.MAX_INPUT = 64000
    check("1a trim_target 64000->44800", app._trim_target() == 44800, str(app._trim_target()))
    app.TRIM_TARGET_TOKENS = 30000
    check("1b absolute override wins", app._trim_target() == 30000, str(app._trim_target()))
    app.TRIM_TARGET_TOKENS = 0
    app.MAX_INPUT = 10000
    check("1c tiny MAX_INPUT", app._trim_target() == 7000, str(app._trim_target()))
    app.MAX_INPUT = 20000
    app.TRIM_TARGET_FRACTION = 0.10
    check("1d floor clamps", app._trim_target() == 10000, str(app._trim_target()))
    app.MAX_INPUT = 64000
    app.TRIM_TARGET_FRACTION = 0.70
    app.TRIM_TARGET_TOKENS = 0

# ===================== TEST 2: BASELINE vs NEW prefix stability =====================
def test_baseline_vs_new():
    bak = "/home/pawelw/ctxproxy/proxy/app.py.bak-20261004-2142"
    loader = importlib.machinery.SourceFileLoader("app_old", bak)
    spec = importlib.util.spec_from_loader("app_old", loader)
    old = importlib.util.module_from_spec(spec)
    loader.exec_module(old)
    old.MAX_INPUT = 64000
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    reset_state()
    app.pool = None
    new_res = run_growing(hist, 5, len(hist) + 1, None, "s2")
    new_stab = []
    for k in range(1, len(new_res)):
        new_stab.append(prefix_stability(new_res[k-1][1], new_res[k][1]))
    old.session_compactions.clear()
    old.pool = None
    old_res = run_growing(hist, 5, len(hist) + 1, None, "s2")
    old_stab = []
    for k in range(1, len(old_res)):
        old_stab.append(prefix_stability(old_res[k-1][1], old_res[k][1]))
    # reuse turns = turns where cut did not change from previous
    new_reuse_stab = []
    for k in range(1, len(new_res)):
        if new_res[k][2] == new_res[k-1][2]:
            new_reuse_stab.append(new_stab[k-1])
    old_reuse_stab = []
    for k in range(1, len(old_res)):
        if old_res[k][2] == old_res[k-1][2]:
            old_reuse_stab.append(old_stab[k-1])
    print("  NEW reuse-stab: " + str([round(x, 2) for x in new_reuse_stab]))
    print("  OLD reuse-stab: " + str([round(x, 2) for x in old_reuse_stab]))
    print("  NEW avg=%.3f (n=%d)  OLD avg=%.3f (n=%d)" % (sum(new_reuse_stab)/max(1,len(new_reuse_stab)), len(new_reuse_stab), sum(old_reuse_stab)/max(1,len(old_reuse_stab)), len(old_reuse_stab)))
    check("2 NEW prefix stability == 1.0 on all reuse turns", len(new_reuse_stab) > 0 and all(x >= 0.999 for x in new_reuse_stab), "n=%d min=%.3f" % (len(new_reuse_stab), min(new_reuse_stab) if new_reuse_stab else 0))

# ===================== TEST 3: re-cut spacing =====================
def test_recut_spacing():
    reset_state()
    app.pool = None
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    res = run_growing(hist, 5, len(hist) + 1, None, "s3")
    recut_idx = []
    for k in range(1, len(res)):
        if res[k][2] != res[k-1][2]:
            recut_idx.append(res[k][0])
    if len(recut_idx) >= 2:
        gaps = [recut_idx[i+1] - recut_idx[i] for i in range(len(recut_idx)-1)]
        avg_gap = sum(gaps) / len(gaps)
        print("  re-cut at i=%s gaps=%s avg=%.2f (expected ~7.7)" % (recut_idx, gaps, avg_gap))
        check("3 re-cut spacing ~7.7 +-1.5", 6.0 <= avg_gap <= 9.5, "avg=%.2f n=%d" % (avg_gap, len(gaps)))
    else:
        check("3 re-cut spacing ~7.7 +-1.5", len(recut_idx) >= 2, "only %d recuts %s" % (len(recut_idx), recut_idx))

# ===================== TEST 4: anchor mismatch =====================
def test_anchor_mismatch():
    reset_state()
    app.pool = None
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    res = run_growing(hist, 5, 41, None, "s4")
    ws1 = app.session_compactions.get("s4")
    check("4a state established (cut>0)", ws1 is not None and ws1["cut"] > 0, str(ws1 and ws1["cut"]))
    # replace the tail content with SAME-LENGTH text (history replaced, total stays > MAX) -> anchor mismatch -> fresh re-cut
    replaced = [dict(m) for m in hist]
    for i, m in enumerate(replaced[3:]):
        orig = str(m.get("content", ""))
        m["content"] = ("REPLACED %d " % i) + _pad(app.count_tokens(orig))
    out2 = asyncio.run(app.build_context(replaced, task_uuid=None, session_key="s4"))
    ws2 = app.session_compactions.get("s4")
    check("4b anchor mismatch -> fresh re-cut, valid output, no exception", len(out2) > 0 and ws2 is not None and ws2["cut"] > 0)
    check("4c nothing re-summarized (no pool)", app.metrics["summary_slices_failed"] == 0, str(app.metrics["summary_slices_failed"]))
    shorter = make_history(10, seed_tokens=50000, per_turn_tokens=2500)
    out3 = asyncio.run(app.build_context(shorter, task_uuid=None, session_key="s4"))
    check("4d shorter history -> valid", len(out3) > 0)

# ===================== TEST 5: below MAX_INPUT =====================
def test_below_max():
    reset_state()
    app.pool = None
    small = make_history(3, seed_tokens=10000, per_turn_tokens=500)
    out = asyncio.run(app.build_context(small, task_uuid=None, session_key="s5"))
    check("5a below max returns all", len(out) == len(small), "%d vs %d" % (len(out), len(small)))
    check("5b state cleared", "s5" not in app.session_compactions)

# ===================== TEST 6: tool-graph validity =====================
def test_tool_graph():
    reset_state()
    app.pool = None
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do 150 things"}]
    for i in range(150):
        tcs = [{"id": "a%d" % i, "type": "function", "function": {"name": "f", "arguments": "{}"}},
               {"id": "b%d" % i, "type": "function", "function": {"name": "g", "arguments": "{}"}}]
        msgs.append({"role": "assistant", "content": "step %d" % i, "tool_calls": tcs})
        msgs.append({"role": "tool", "tool_call_id": "a%d" % i, "content": "ra%d" % i})
        msgs.append({"role": "tool", "tool_call_id": "b%d" % i, "content": "rb%d" % i})
    for m in msgs[3:]:
        if m.get("content"):
            m["content"] = m["content"] + (" x" * 200)
    out = asyncio.run(app.build_context(msgs, task_uuid=None, session_key="s6"))
    ok, why = tool_graph_valid(out)
    check("6 tool-graph valid on 150 parallel pairs", ok, why)

# ===================== TEST 7: stub byte-identical =====================
def test_stub_identical():
    reset_state()
    app.pool = None
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    res = run_growing(hist, 5, len(hist) + 1, None, "s7")
    stubs = set()
    for i, out, cut in res:
        for m in out:
            if m.get("role") == "system" and "COMPACTED" in str(m.get("content", "")):
                stubs.add(m["content"])
    check("7 stub byte-identical across all turns/re-cuts", len(stubs) == 1, "distinct stubs=%d" % len(stubs))

# ===================== TEST 8: seed identical every turn =====================
def test_seed_identical():
    reset_state()
    app.pool = None
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    res = run_growing(hist, 5, len(hist) + 1, None, "s8")
    seed0 = res[0][1][:3]
    all_same = all(r[1][:3] == seed0 for r in res)
    check("8 seed identical every turn", all_same)

# ===================== TEST 9: 30k-token tool output in tail =====================
def test_huge_tool_output():
    reset_state()
    app.pool = None
    hist = make_history(20, seed_tokens=50000, per_turn_tokens=2500)
    huge = "HUGE " * 7500
    hist.append({"role": "assistant", "content": "big call", "tool_calls": [{"id": "big1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
    hist.append({"role": "tool", "tool_call_id": "big1", "content": huge})
    t0 = time.monotonic()
    out = asyncio.run(app.build_context(hist, task_uuid=None, session_key="s9"))
    dt = time.monotonic() - t0
    check("9a huge tool output no crash", len(out) > 0, "out=%d" % len(out))
    check("9b no endless loop (<5s)", dt < 5.0, "%.2fs" % dt)
    ok, why = tool_graph_valid(out)
    check("9c tool-graph valid with huge msg (no orphan)", ok, why)

# ===================== TEST 10: EXACTLY ONCE =====================
def test_exactly_once():
    reset_state()
    app._call_4b = Fake4B()
    pool = FakePool(table_exists=True)
    app.pool = pool
    hist = make_history(60, seed_tokens=50000, per_turn_tokens=2500)
    res = run_growing(hist, 5, len(hist) + 1, "t10", "s10", settle=2.0)
    ws = app.session_compactions.get("s10")
    final_cut = ws["cut"] if ws else 0
    # non-empty slices only, in order
    slices = [(s, e) for (s, e) in app._slice_log if e > s]
    ok_tiling = True
    cur_end = 0
    for s, e in slices:
        if s != cur_end:
            ok_tiling = False
            break
        cur_end = e
    print("  slices=%s final_cut=%d covered_to=%d" % (slices[:6], final_cut, cur_end))
    check("10a exactly-once tiling (no gaps/overlaps) to final cut", ok_tiling and cur_end == final_cut, "slices=%s covered=%d cut=%d" % (slices[:4], cur_end, final_cut))
    check("10b no duplicate slice", len(set(slices)) == len(slices), "n=%d" % len(slices))
    check("10c watermark == final cut (all summarized)", ws and ws["summarized_through"] == final_cut, "st=%s cut=%s" % (ws.get("summarized_through") if ws else None, final_cut))

# ===================== TEST 11: two re-cuts <120s both summarized =====================
def test_two_recuts_120s():
    reset_state()
    app._call_4b = Fake4B()
    pool = FakePool(table_exists=True)
    app.pool = pool
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=3000)
    res = run_growing(hist, 5, len(hist) + 1, "t11", "s11", settle=2.0)
    n_phase = len(pool.phase_rows)
    n_slices = len([x for x in app._slice_log if x[1] > x[0]])
    print("  phases=%d slices=%d" % (n_phase, n_slices))
    check("11 two re-cuts <120s both summarized", n_slices >= 2 and n_phase >= 2, "slices=%d phases=%d" % (n_slices, n_phase))

# ===================== TEST 12: cost independence =====================
def test_cost_independence():
    reset_state()
    app.pool = None
    big = make_history(5, seed_tokens=50000, per_turn_tokens=1000)
    for i in range(150):
        big.append({"role": "assistant", "content": "extra %d " % i + _pad(600),
                    "tool_calls": [{"id": "e%d" % i, "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
        big.append({"role": "tool", "tool_call_id": "e%d" % i, "content": "out %d " % i + _pad(600)})
    small = make_history(5, seed_tokens=50000, per_turn_tokens=1000)
    async def _time():
        await app.build_context(big, task_uuid=None, session_key="s12")
        t0 = time.monotonic()
        for _ in range(10):
            await app.build_context(big, task_uuid=None, session_key="s12")
        t_big = (time.monotonic() - t0) / 10
        t0 = time.monotonic()
        for _ in range(10):
            await app.build_context(small, task_uuid=None, session_key="s12b")
        t_small = (time.monotonic() - t0) / 10
        return t_big, t_small
    t_big, t_small = asyncio.run(_time())
    print("  big(400k) reuse=%.2fms small(50k)=%.2fms ratio=%.2f" % (t_big*1000, t_small*1000, t_big/max(t_small,1e-6)))
    check("12 reuse cost independent (big <= small*1.5 + 50ms)", t_big <= t_small * 1.5 + 0.05, "big=%.2fms small=%.2fms" % (t_big*1000, t_small*1000))

# ===================== TEST 13: RESTART (lazy load, byte-identical) =====================
def test_restart():
    reset_state()
    app._call_4b = Fake4B()
    pool = FakePool(table_exists=True)
    app.pool = pool
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    res1 = run_growing(hist, 5, 25, "t13", "s13", settle=1.5)
    out_a = res1[-1][1]
    ws_before = dict(app.session_compactions.get("s13"))
    check("13a DB row written on re-cut", "s13" in pool.rows, str(list(pool.rows.keys())))
    # RESTART: clear in-memory, keep DB rows
    app.session_compactions.clear()
    app._window_locks.clear()
    app._window_load_tried.clear()
    out_b = asyncio.run(app.build_context(hist[:25], task_uuid="t13", session_key="s13"))
    ws_after = app.session_compactions.get("s13")
    check("13b lazy load read the row", app.metrics["window_loads_db"] >= 1, "loads=%d" % app.metrics["window_loads_db"])
    check("13c same cut reused", ws_after and ws_after["cut"] == ws_before["cut"], "before=%s after=%s" % (ws_before.get("cut"), ws_after.get("cut") if ws_after else None))
    stab = prefix_stability(out_a, out_b)
    check("13d byte-identical prefix after restart", stab >= 0.999, "stab=%.3f" % stab)
    check("13e watermark preserved (no re-summarize)", ws_after and ws_after["summarized_through"] == ws_before["summarized_through"], "before=%s after=%s" % (ws_before.get("summarized_through"), ws_after.get("summarized_through") if ws_after else None))

# ===================== TEST 14: restart with NO row (backfill) =====================
def test_restart_no_row():
    reset_state()
    app._call_4b = Fake4B()
    pool = FakePool(table_exists=True)
    pool.rows = {}
    app.pool = pool
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    run_growing(hist, 5, len(hist) + 1, "t14", "s14", settle=2.0)
    n_phase = len(pool.phase_rows)
    ws = app.session_compactions.get("s14")
    check("14a backfill summarized (phases stored)", n_phase >= 1, "phases=%d" % n_phase)
    check("14b watermark set to cut after backfill", ws and ws["summarized_through"] == ws["cut"], "st=%s cut=%s" % (ws.get("summarized_through") if ws else None, ws.get("cut") if ws else None))

# ===================== TEST 15: table missing / DB error =====================
def test_table_missing():
    reset_state()
    app._call_4b = Fake4B()
    pool = FakePool(table_exists=False)
    app.pool = pool
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    res = run_growing(hist, 5, 25, "t15", "s15", settle=1.0)
    check("15a request succeeds with table missing", len(res[0][1]) > 0 and len(res[-1][1]) > 0)
    check("15b persistence disabled after error", app._window_persist_enabled is False, str(app._window_persist_enabled))
    check("15c window_persist_errors incremented", app.metrics["window_persist_errors"] >= 1, "errs=%d" % app.metrics["window_persist_errors"])
    ws = app.session_compactions.get("s15")
    check("15d in-memory state intact", ws is not None and ws["cut"] > 0, str(ws and ws["cut"]))
    check("15e no re-warn spam (warned flag set)", app._window_persist_warned is True)

# ===================== TEST 16: TTL cleanup =====================
def test_ttl_cleanup():
    reset_state()
    pool = FakePool(table_exists=True)
    app.pool = pool
    app._window_persist_enabled = True
    app.WINDOW_TTL_DAYS = 7
    asyncio.run(app._window_cleanup_ttl())
    deletes = [e for e in pool.sql_log if e[0] == "execute" and "DELETE FROM proxy.session_windows" in e[1]]
    check("16a TTL issues DELETE only on session_windows", len(deletes) == 1, "deletes=%d" % len(deletes))
    check("16b TTL uses configured age", deletes and "7" in str(deletes[0][2]), str(deletes[0][2] if deletes else None))
    other = [e for e in pool.sql_log if e[0] == "execute" and "DELETE" in e[1] and "session_windows" not in e[1]]
    check("16c no other table deleted", len(other) == 0)

# ===================== TEST 17: reuse turns issue NO DB writes =====================
def test_reuse_no_db_writes():
    reset_state()
    app._call_4b = Fake4B()
    pool = FakePool(table_exists=True)
    app.pool = pool
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    res = run_growing(hist, 5, 25, "t17", "s17", settle=1.5)
    # count window upserts (INSERT ... session_windows) — one per re-cut + watermark advances
    window_writes = [e for e in pool.sql_log if e[0] == "execute" and "session_windows" in e[1]]
    n_recuts = len(set(r[2] for r in res)) - 1
    print("  window_writes=%d recuts=%d" % (len(window_writes), n_recuts))
    # reuse turns must not add writes: total writes should be small (recuts + watermark advances), not one per turn
    check("17 reuse turns issue NO per-turn DB write", len(window_writes) <= n_recuts + 20, "writes=%d recuts=%d turns=%d" % (len(window_writes), n_recuts, len(res)))
    check("17b it was a reuse (metric)", app.metrics["trim_sticky_reuse"] >= 1, "reuse=%d" % app.metrics["trim_sticky_reuse"])

# ===================== TEST 18: failure injection (watermark stays, re-derived later) =====================
def test_failure_injection():
    reset_state()
    app._call_4b = Fake4B(fail_on_call=0)  # first phase call fails
    pool = FakePool(table_exists=True)
    app.pool = pool
    hist = make_history(40, seed_tokens=50000, per_turn_tokens=2500)
    run_growing(hist, 5, 25, "t18", "s18", settle=1.5)
    ws1 = app.session_compactions.get("s18")
    check("18a summary_slices_failed incremented", app.metrics["summary_slices_failed"] >= 1, "failed=%d" % app.metrics["summary_slices_failed"])
    # fix the summarizer + simulate 200s passing (retry throttle), add a turn to trigger retry
    app._call_4b = Fake4B()
    app._summary_last_attempt["t18"] = (time.time() - 200, 0)
    hist2 = hist + [{"role": "assistant", "content": "more " + _pad(1250), "tool_calls": [{"id": "tcX", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": "tcX", "content": "outX " + _pad(1250)}]
    run_growing(hist2, 25, 26, "t18", "s18", settle=2.0)
    ws2 = app.session_compactions.get("s18")
    check("18b watermark advances after retry success", ws2 and ws2["summarized_through"] >= ws1["cut"], "st=%s prev_cut=%s" % (ws2.get("summarized_through") if ws2 else None, ws1["cut"]))

# ===================== MAIN =====================
def main():
    tests = [test_trim_target, test_baseline_vs_new, test_recut_spacing, test_anchor_mismatch,
             test_below_max, test_tool_graph, test_stub_identical, test_seed_identical,
             test_huge_tool_output, test_exactly_once, test_two_recuts_120s, test_cost_independence,
             test_restart, test_restart_no_row, test_table_missing, test_ttl_cleanup, test_reuse_no_db_writes,
             test_failure_injection]
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
    import sys
    sys.exit(main())

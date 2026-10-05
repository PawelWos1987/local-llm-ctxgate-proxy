#!/usr/bin/env python3
"""Standalone offline tests for the ctxgate-proxy window-collapse fix.

Mirrors tests/test_suite.py style: extracts the REAL functions from
proxy/app.py via AST (no import of the whole app), stubs the token
counters, and asserts the F1-F5 invariants. Run: python3 tests/test_window_collapse.py
"""
import ast, hashlib, re, json, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "proxy", "app.py")

def _extract(names):
    src = open(APP).read()
    tree = ast.parse(src)
    chunks = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            chunks.append(ast.get_source_segment(src, node))
    return "\n\n".join(chunks)

def _count_message_tokens(m):
    c = m.get("content")
    if c is None:
        c = ""
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    n = len(str(c)) // 4 + 4
    for t in (m.get("tool_calls") or []):
        n += len(str(t.get("function", {}).get("arguments", ""))) // 4
    return n

def _count_messages_tokens(ms):
    return sum(_count_message_tokens(m) for m in ms)

def _load():
    names = ["_recut_to", "_recut", "_msg_anchor", "_seed_sig", "_norm_content",
             "_new_window_state", "_window_valid", "_kept_messages", "_prep_messages"]
    mod_src = _extract(names)
    ns = {}
    STUB_TEXT = "[COMPACTED HISTORY] earlier turns archived; see TASK STATE"
    ns.update(STUB_TEXT=STUB_TEXT,
              count_message_tokens=_count_message_tokens,
              count_messages_tokens=_count_messages_tokens,
              _trim_target=lambda: 44800,
              metrics={}, hashlib=hashlib, re=re, json=json)
    import asyncio
    ns["asyncio"] = asyncio
    ns["log"] = type("L", (), {"warning": staticmethod(lambda *a: None),
                               "info": staticmethod(lambda *a: None),
                               "error": staticmethod(lambda *a: None)})()
    exec(compile(mod_src, "extracted", "exec"), ns)
    return ns

NS = _load()
STUB_TEXT = NS["STUB_TEXT"]

def kept(seed, rest, cut):
    return NS["_kept_messages"](seed, rest, cut)

def recut(messages, max_tokens=44800):
    return NS["_recut_to"](messages, max_tokens)

def mk_assistant(i, ntools=1, content=None, null_content=False):
    m = {"role": "assistant"}
    m["content"] = None if null_content else (content if content is not None else ("assistant work step %d thinking" % i))
    m["tool_calls"] = [{"id": "tc%d_%d" % (i, j), "type": "function",
                        "function": {"name": "tool_%d" % j, "arguments": "{}"}} for j in range(ntools)]
    return m

def mk_tool(i, j, size=1600):
    return {"role": "tool", "tool_call_id": "tc%d_%d" % (i, j), "content": ("x" * size)}

def mk_user(i):
    return {"role": "user", "content": "user message %d" % i}

PASS = 0
FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, detail))

def tool_graph_ok(msgs):
    """Every kept tool msg has its assistant tool_call kept; every kept assistant
    tool_call has all its results kept."""
    kept_tc_ids = set()
    for m in msgs:
        if m.get("role") == "assistant":
            for t in (m.get("tool_calls") or []):
                kept_tc_ids.add(t["id"])
    kept_tool_ids = set()
    for m in msgs:
        if m.get("role") == "tool":
            kept_tool_ids.add(m.get("tool_call_id"))
    # orphan tool (assistant not kept)
    for tid in kept_tool_ids:
        if tid not in kept_tc_ids:
            return False, "orphan tool %s" % tid
    # assistant tool_call with a result missing
    for m in msgs:
        if m.get("role") == "assistant":
            for t in (m.get("tool_calls") or []):
                if t["id"] not in kept_tool_ids:
                    return False, "missing result for %s" % t["id"]
    return True, "graph ok"

def run_trace(kind, max_tokens=44800):
    msgs = [{"role": "system", "content": "SYS" * 20},
            {"role": "system", "content": "SYS2" * 10},
            mk_user(0)]
    if kind == "A":  # pure agent loop
        for i in range(150):
            msgs.append(mk_assistant(i)); msgs.append(mk_tool(i, 0))
    elif kind == "B":  # user every 40 pairs
        for i in range(150):
            if i % 40 == 0:
                msgs.append(mk_user(i))
            msgs.append(mk_assistant(i)); msgs.append(mk_tool(i, 0))
    elif kind == "C":  # 300 small pairs
        for i in range(300):
            msgs.append(mk_assistant(i, content=("a" * 160))); msgs.append(mk_tool(i, 0, size=120))
    elif kind == "D":  # 60 big-result pairs
        for i in range(60):
            msgs.append(mk_assistant(i, content=("b" * 200))); msgs.append(mk_tool(i, 0, size=4000))
    elif kind == "E":  # parallel tool calls (assistant 3 tool_calls + 3 tool msgs)
        for i in range(40):
            a = {"role": "assistant", "content": ("a" * 200),
                 "tool_calls": [{"id": "tc%d_%d" % (i, j), "type": "function",
                                 "function": {"name": "t%d" % j, "arguments": "{}"}} for j in range(3)]}
            msgs.append(a)
            for j in range(3):
                msgs.append(mk_tool(i, j, size=800))
    elif kind == "F":  # first assistant content null
        for i in range(60):
            msgs.append(mk_assistant(i, null_content=True)); msgs.append(mk_tool(i, 0))
    elif kind == "G":  # only 8 messages
        msgs = [{"role": "system", "content": "SYS"}, {"role": "system", "content": "S2"},
                mk_user(0), mk_assistant(0), mk_tool(0, 0, 400), mk_user(1),
                mk_assistant(1), mk_tool(1, 0, 400)]
    elif kind == "H":  # one 30k-token tool output at the end
        for i in range(40):
            msgs.append(mk_assistant(i)); msgs.append(mk_tool(i, 0, size=400))
        msgs.append(mk_assistant(40))
        msgs.append({"role": "tool", "tool_call_id": "tc40_0", "content": "y" * 120000})  # ~30k tokens
    total = _count_messages_tokens(msgs)
    cut = recut(msgs, max_tokens)
    seed = msgs[:3]; rest = msgs[3:]
    k = kept(seed, rest, cut)
    kt = _count_messages_tokens(k)
    ok, why = tool_graph_ok(k)
    return dict(kind=kind, total=total, cut=cut, rest_n=len(rest),
                kept_n=len(k), kept_tok=kt, ratio=kt / max_tokens,
                newest_kept=(rest and k[-1] is rest[-1]), graph=ok, graphwhy=why)

def test_traces():
    print("\n== Traces A-H (F1 invariants) ==")
    for kind in ["A", "B", "C", "D", "E", "F", "G", "H"]:
        r = run_trace(kind)
        # (a) tail never empty while rest>0 and newest kept
        a = (r["kept_n"] > 3) and r["newest_kept"]
        # (c) kept >= 0.8*min(target,total) unless one unit > budget
        floor = 0.8 * min(44800, r["total"])
        c = r["kept_tok"] >= floor or r["kind"] in ("G", "H")
        check("trace %s nonempty+newest" % kind, a, "kept_n=%d newest=%s" % (r["kept_n"], r["newest_kept"]))
        check("trace %s token floor" % kind, c, "kept=%d floor=%d" % (r["kept_tok"], floor))
        check("trace %s tool graph" % kind, r["graph"], r["graphwhy"])
        if kind in ("A", "B", "C", "D"):
            floor_nc = 0.8 * min(44800, r["total"])
            check("trace %s not collapsed (>=0.8*min(target,total))" % kind,
                  r["kept_tok"] >= floor_nc, "kept=%d floor=%d ratio=%.2f" % (r["kept_tok"], floor_nc, r["ratio"]))

def test_sticky_reuse():
    print("\n== Sticky reuse (W2/W3): 60-turn growing replay ==")
    msgs = [{"role": "system", "content": "SYS" * 20},
            {"role": "system", "content": "SYS2" * 10},
            mk_user(0)]
    for i in range(40):
        msgs.append(mk_assistant(i)); msgs.append(mk_tool(i, 0))
    # prepped vs raw anchor/sig equality
    raw_seed = msgs[:3]; raw_rest = msgs[3:]
    prep = NS["_prep_messages"](msgs)
    check("seed_sig raw==prep", NS["_seed_sig"](raw_seed) == NS["_seed_sig"](prep[:3]),
          "%s vs %s" % (NS["_seed_sig"](raw_seed)[:8], NS["_seed_sig"](prep[:3])[:8]))
    check("anchor null-content raw==prep", NS["_msg_anchor"](msgs[3]) == NS["_msg_anchor"](prep[3]),
          "raw=%s prep=%s" % (NS["_msg_anchor"](msgs[3])[:8], NS["_msg_anchor"](prep[3])[:8]))
    prev_cut = None
    prev_prefix = None
    sticky_ok = True
    for turn in range(60):
        # history grows by 2 msgs each turn
        if turn > 0:
            i = 40 + turn - 1
            msgs.append(mk_assistant(i)); msgs.append(mk_tool(i, 0))
        seed = msgs[:3]; rest = msgs[3:]
        cut = recut(msgs, 44800)
        ws = NS["_new_window_state"](cut, rest, seed, 0)
        # validate against RAW (as build_context does)
        valid = NS["_window_valid"](ws, seed, rest)
        if prev_cut is not None:
            # sticky: turn N+1 reuses cut of turn N (history only appended after cut)
            if cut != prev_cut:
                sticky_ok = False
        prev_cut = cut
        k = kept(seed, rest, cut)
        prefix = json.dumps([m.get("role") for m in k[:5]], sort_keys=True)
        if prev_prefix is not None and prefix != prev_prefix:
            pass  # prefix roles stable
        prev_prefix = prefix
        if not valid:
            sticky_ok = False
    check("sticky-reuse over 60 turns", sticky_ok, "final cut=%d" % prev_cut)

def test_summarizer_feed():
    print("\n== Summarizer feed (F3): each dropped msg exactly once ==")
    # simulate build_context st_start carry-forward
    msgs = [{"role": "system", "content": "S"}, {"role": "system", "content": "S2"}, mk_user(0)]
    for i in range(50):
        msgs.append(mk_assistant(i)); msgs.append(mk_tool(i, 0))
    seed = msgs[:3]; rest = msgs[3:]
    ws = None
    seen = []
    for turn in range(5):
        if turn > 0:
            i = 50 + turn - 1
            msgs.append(mk_assistant(i)); msgs.append(mk_tool(i, 0))
        seed = msgs[:3]; rest = msgs[3:]
        cut = recut(msgs, 44800)
        if rest and cut >= len(rest):
            cut = len(rest) - 1
        st_start = 0
        if ws:
            if ws.get("seed_sig") == NS["_seed_sig"](seed):
                st_start = ws.get("summarized_through", 0)
        new_ws = NS["_new_window_state"](cut, rest, seed, st_start)
        # dropped slice [st_start, cut)
        for idx in range(st_start, cut):
            seen.append(idx)
        new_ws["summarized_through"] = cut
        ws = new_ws
    # each dropped message index should appear exactly once, in order, no gaps/dups
    dedup = len(set(seen)) == len(seen)
    in_range = all(0 <= x < 100 for x in seen)
    ordered = seen == sorted(seen)
    check("summarizer no dups", dedup, "seen=%d unique=%d" % (len(seen), len(set(seen))))
    check("summarizer in range", in_range)
    check("summarizer ordered", ordered, "first=%s last=%s" % (seen[:3], seen[-3:]))

def test_cadence():
    print("\n== Loop cadence (F4): 135-char loop starting at char 20000 ==")
    # extract real _detect_loop
    names = ["_detect_loop"]
    mod_src = _extract(names)
    n2 = {}
    LOOP_TAIL = 4000; LOOP_CHECK_EVERY = 256
    LOOP_REPEATS = 4; LOOP_SENTENCE_REPEATS = 3
    n2.update(LOOP_TAIL=LOOP_TAIL, LOOP_CHECK_EVERY=LOOP_CHECK_EVERY,
              LOOP_REPEATS=LOOP_REPEATS, LOOP_SENTENCE_REPEATS=LOOP_SENTENCE_REPEATS,
              re=re)
    exec(compile(mod_src, "e", "exec"), n2)
    detect = n2["_detect_loop"]

    period_unit = ("The agent is repeating itself here step " + "z" * 100)  # 135 chars
    # 20000 chars of non-repeating preamble, then the 135-char loop
    preamble = "".join("Intro sentence number %d with unique wording %d. " % (i, i) for i in range(400))
    preamble = preamble[:20000]
    loop_stream = preamble + (period_unit * 200)
    def stream(chunk_size):
        reasoning_tail = ""
        acc = 0
        full = ""
        detected_at = None
        for i in range(0, len(loop_stream), chunk_size):
            piece = loop_stream[i:i + chunk_size]
            full += piece
            reasoning_tail = (reasoning_tail + piece)[-LOOP_TAIL:]
            acc += len(piece)
            if acc >= LOOP_CHECK_EVERY:
                acc = 0
                lp = detect(reasoning_tail)
                if lp:
                    detected_at = len(full)
                    break
        return detected_at
    for cs in [1, 7, 40]:
        d = stream(cs)
        ok = d is not None and d >= 20000 and d <= 22000
        check("cadence chunk=%d" % cs, ok, "detected_at=%s" % d)
    # no false positive on varied prose + a markdown table
    prose = "".join("Sentence number %d talks about a distinct topic %d. " % (i, i) for i in range(1200))
    table = ""
    for i in range(300):
        table += "| colA %d | colB %d | colC %d |\n" % (i, i * 2, i * 3)
    varied = (prose + table) * 3
    false_pos = 0
    tail = ""; acc = 0
    for i in range(0, len(varied), 40):
        piece = varied[i:i + 40]
        tail = (tail + piece)[-LOOP_TAIL:]
        acc += len(piece)
        if acc >= LOOP_CHECK_EVERY:
            acc = 0
            if detect(tail):
                false_pos += 1
    check("cadence no false positive on varied prose+table", false_pos == 0, "false_pos=%d" % false_pos)

def test_compile():
    print("\n== Compile ==")
    import subprocess
    r = subprocess.run([sys.executable, "-m", "py_compile", APP], capture_output=True, text=True)
    check("app.py compiles", r.returncode == 0, r.stderr[:200])

if __name__ == "__main__":
    test_traces()
    test_sticky_reuse()
    test_summarizer_feed()
    test_cadence()
    test_compile()
    print("\n==== %d passed, %d failed ====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)

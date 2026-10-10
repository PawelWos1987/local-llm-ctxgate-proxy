#!/usr/bin/env python3
"""Regression checks for the COMPACTION_NOTE patch. Self-contained: reads proxy/app.py as
text and executes only a few pure functions. Runs with `python3` or with pytest."""
import ast, hashlib, os, sys

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "proxy", "app.py")
SRC = open(APP, encoding="utf-8").read()
TREE = ast.parse(SRC)

FUNCS = {"_note_tok", "_with_compaction_note", "_norm_content", "_normalize_system_messages",
         "_kept_messages", "_pinned_user_copy", "_newest_user_idx", "_make_pinned_copy",
         "_protected_indices"}
CONSTS = {"COMPACTION_NOTE", "_NOTE_TOK_CACHE"}


def load():
    ns = {"os": os, "hashlib": hashlib,
          "PINNED_USER_MAX_CHARS": 4000, "PINNED_USER_FAR_CHARS": 1000,
          "PINNED_USER_FAR_THRESHOLD": 10,
          "count_tokens": lambda t: len(t) // 4,
          "protected_tool_groups": lambda w: set()}
    code = ""
    for n in TREE.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in CONSTS for t in n.targets):
            code += ast.get_source_segment(SRC, n) + chr(10)
        if isinstance(n, ast.FunctionDef) and n.name in FUNCS:
            code += ast.get_source_segment(SRC, n) + chr(10)
    exec(code, ns)
    return ns


def session():
    seed = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"}]
    rest = []
    for i in range(1, 41):
        rest.append({"role": "user" if i % 2 == 1 else "assistant", "content": "m%d" % i})
    return seed, rest


def test_stub_removed_from_source():
    assert "STUB_TEXT" not in SRC


def test_message0_identical_before_and_after_cut():
    ns = load()
    W, N, K = ns["_with_compaction_note"], ns["_normalize_system_messages"], ns["_kept_messages"]
    seed, rest = session()
    before_cut = N(W(seed + rest))
    after_cut = N(W(K(seed, rest, 20)))
    assert before_cut[0] == after_cut[0]
    assert before_cut[1:3] == after_cut[1:3]
    assert after_cut[0]["content"].count(ns["COMPACTION_NOTE"]) == 1
    assert after_cut[0]["content"].endswith(ns["COMPACTION_NOTE"])


def test_no_system_message_after_seed_in_kept_window():
    ns = load()
    seed, rest = session()
    for cut in (5, 20, 39, 40):
        kept = ns["_kept_messages"](seed, rest, cut)
        assert all(m["role"] != "system" for m in kept[1:]), cut


def test_pinned_copy_sits_at_index_3():
    ns = load()
    seed, rest = session()
    newest_user = ns["_newest_user_idx"](rest)
    kept = ns["_kept_messages"](seed, rest, newest_user + 1)
    assert kept[3].get("ctxgate_pinned") is True
    kept2 = ns["_kept_messages"](seed, rest, 5)
    assert not kept2[3].get("ctxgate_pinned")
    assert kept2[3] == rest[5]


def test_helper_idempotent_and_non_mutating():
    ns = load()
    W = ns["_with_compaction_note"]
    seed, rest = session()
    snapshot = [dict(m) for m in seed]
    once = W(seed)
    assert seed == snapshot
    assert W(once) == once
    assert seed[0]["content"] == "SYS"


def test_helper_edge_cases():
    ns = load()
    W = ns["_with_compaction_note"]
    no_sys = W([{"role": "user", "content": "x"}])
    assert no_sys[0]["role"] == "system" and no_sys[0]["content"] == ns["COMPACTION_NOTE"]
    listed = W([{"role": "system", "content": [{"type": "text", "text": "A"}]}])
    assert isinstance(listed[0]["content"], str) and listed[0]["content"].startswith("A")
    assert W([]) == []


def test_note_is_constant_text():
    ns = load()
    note = ns["COMPACTION_NOTE"]
    assert isinstance(note, str) and len(note) > 20
    assert ns["_note_tok"]() > 0


def test_protected_indices_ignores_mid_history_system_message():
    ns = load()
    work = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
            {"role": "system", "content": "stray"},
            {"role": "user", "content": "p", "ctxgate_pinned": True},
            {"role": "assistant", "content": "x"}]
    assert ns["_protected_indices"](work) == {0, 1, 2, 4}


def test_note_applied_once_in_handler_and_never_in_build_context():
    calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_with_compaction_note"]
    assert len(calls) == 1, len(calls)  # exactly one call site: the request handler
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in (
                "build_context", "_prep_messages", "_kept_messages", "_new_window_state",
                "_window_valid", "_seed_sig"):
            assert "_with_compaction_note" not in ast.get_source_segment(SRC, n), n.name
    a = SRC.index("built = _repair_dangling_tool_calls(built)")
    b = SRC.index("built = _with_compaction_note(built)")
    c = SRC.index("fp = hashlib.sha256(_prefix_raw(built)")
    d = SRC.index("_prefix_diag(session_key, vllm_body.get(")
    assert a < b < c < d


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                failed += 1
                print("FAIL", name, repr(e))
    print("ALL PASS" if failed == 0 else "FAILURES: %d" % failed)
    sys.exit(1 if failed else 0)

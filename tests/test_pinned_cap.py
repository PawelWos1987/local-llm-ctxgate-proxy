#!/usr/bin/env python3
"""Test that _make_pinned_copy respects CTXGATE_PINNED_USER_MAX_CHARS (default 16000).
First 300 chars always verbatim; total capped at PINNED_USER_MAX_CHARS."""
import os, sys, ast

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

def _load():
    names = ["_make_pinned_copy", "_norm_content"]
    mod_src = _extract(names)
    ns = {}
    import hashlib
    ns.update(hashlib=hashlib,
              PINNED_USER_MAX_CHARS=16000, PINNED_USER_FAR_CHARS=2000,
              PINNED_USER_FAR_THRESHOLD=8)
    exec(compile(mod_src, "extracted", "exec"), ns)
    return ns

NS = _load()

def test_pinned_copy_within_cap():
    long_msg = {"role": "user", "content": "A" * 50000}
    copy = NS["_make_pinned_copy"](long_msg)
    text = copy["content"]
    assert text[:300] == "A" * 300, "first 300 chars must be verbatim"
    assert len(text) <= 16000 + 30, f"cap exceeded: {len(text)}"
    assert "[...middle omitted by ctxgate...]" in text, "truncation marker missing"

def test_pinned_copy_short_unchanged():
    short_msg = {"role": "user", "content": "B" * 200}
    copy = NS["_make_pinned_copy"](short_msg)
    assert copy["content"] == "B" * 200

def test_pinned_copy_exactly_at_cap():
    at_cap = {"role": "user", "content": "C" * 16000}
    copy = NS["_make_pinned_copy"](at_cap)
    assert copy["content"] == "C" * 16000, "at-cap message should not be truncated"

if __name__ == "__main__":
    test_pinned_copy_within_cap()
    test_pinned_copy_short_unchanged()
    test_pinned_copy_exactly_at_cap()
    print("PASS: pinned user copy cap")

#!/usr/bin/env python3
"""Shrink group-stability test: grow history by 2 msgs/step for 10 steps with the
shrink ACTIVE (an oversized seed forces elide+drop every step). Report per-step
prefix diffs of the first N kept messages (prefix-cache stability) and assert no
orphan tool messages survive (assistant+tool_calls and ALL its tool results stay
together)."""
import os, ast, collections

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
    names = ["_emergency_shrink", "_protected_indices", "protected_tool_groups",
             "_elided_tool_content", "_norm_content", "_msg_anchor", "_newest_user_idx",
             "_make_pinned_copy", "_pinned_user_copy", "_recut_to", "_recut", "_trim_target"]
    mod_src = _extract(names)
    ns = {}
    ns.update(
        count_message_tokens=_count_message_tokens,
        count_messages_tokens=_count_messages_tokens,
        _trim_target=lambda: 40600,
        MAX_CONTEXT=84000, MAX_INPUT=58000, MAX_OUTPUT=22500,
        SAFETY_MARGIN=3500, MIN_OUTPUT=16000,
        PINNED_USER_MAX_CHARS=16000,
        TRIM_TARGET_TOKENS=0, TRIM_TARGET_FRACTION=0.70,
        TRIM_TARGET_FLOOR=20000,
        metrics=collections.defaultdict(int),
    )
    import hashlib, re, json
    ns.update(hashlib=hashlib, re=re, json=json)
    ns["log"] = type("L", (), {
        "warning": staticmethod(lambda *a: None),
        "info": staticmethod(lambda *a: None),
        "error": staticmethod(lambda *a: None),
    })()
    exec(compile(mod_src, "extracted", "exec"), ns)
    return ns

NS = _load()
N_PREFIX = 5
CEILING = 20000

def _mk_pair(step):
    """2 messages: 1 assistant (1 tool_call) + its tool result (~1000 tokens)."""
    return [
        {"role": "assistant", "content": f"step {step} reasoning",
         "tool_calls": [{"id": f"tc{step}_0", "type": "function",
                         "function": {"name": "do_thing", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": f"tc{step}_0",
         "content": "x" * 4000},  # ~1004 tokens
    ]

def _has_parent_tool(kept, i):
    for j in range(i - 1, -1, -1):
        if kept[j].get("role") == "assistant" and kept[j].get("tool_calls"):
            return True
        if kept[j].get("role") in ("user", "system"):
            return False
    return False

def test_shrink_prefix_stability():
    # Oversized seed (system ~10k + user ~1k + assistant ~0.5k) so shrink is ACTIVE
    # from step 0 (total starts ~11.5k, well over CEILING as pairs are added).
    history = [
        {"role": "system", "content": "S" * 40000},   # ~10004 tokens
        {"role": "user", "content": "U" * 4000},      # ~1004 tokens
        {"role": "assistant", "content": "A" * 2000}, # ~504 tokens
    ]
    prev_kept = None
    report = []
    active_steps = 0
    for step in range(10):
        history.extend(_mk_pair(step))
        before = _count_messages_tokens(history)
        kept = NS["_emergency_shrink"](list(history), CEILING)
        after = _count_messages_tokens(kept)
        if before > CEILING:
            active_steps += 1
        # no orphan tool messages
        for i, m in enumerate(kept):
            if m.get("role") == "tool":
                assert _has_parent_tool(kept, i), f"step {step}: ORPHAN tool at kept[{i}]"
        if prev_kept is not None:
            n = min(N_PREFIX, len(prev_kept), len(kept))
            diffs = sum(
                1 for i in range(n)
                if prev_kept[i].get("role") != kept[i].get("role")
                or str(prev_kept[i].get("content", ""))[:100] != str(kept[i].get("content", ""))[:100]
            )
            report.append(f"step={step:2d} before={before:6d} after={after:6d} kept={len(kept):3d} prefix_diffs={diffs}/{n}")
        else:
            report.append(f"step={step:2d} before={before:6d} after={after:6d} kept={len(kept):3d} (baseline)")
        prev_kept = list(kept)
    print("\n".join(report))
    print(f"shrink ACTIVE on {active_steps}/10 steps")
    assert active_steps >= 1, "shrink never activated - test is vacuous"
    for line in report[1:]:
        d = int(line.split("prefix_diffs=")[1].split("/")[0])
        assert d <= 2, f"too many prefix changes (prefix-cache unstable): {line}"

if __name__ == "__main__":
    test_shrink_prefix_stability()
    print("PASS: shrink group-stability")

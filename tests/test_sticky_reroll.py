"""Regression: sticky-window fast path must re-cut (not emergency-shrink) when
the kept window exceeds the ceiling. Fixes the 'emergency shrink every request'
bug that killed the vLLM prefix cache (cut stuck at 17, oldest groups dropped
per call, first kept message changed every turn).

Uses the FakePool / direct-import style from test_rolling_window.py.
No network, no DB, no live server.
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

# Force the len//4 tokenizer fallback (no tiktoken / tokenizers dependency).
os.environ["CTXGATE_QWEN_TOKENIZER"] = " "
os.environ["CTXGATE_MAX_INPUT"] = "58000"
os.environ["CTXGATE_TRIM_TARGET_FRACTION"] = "0.70"
os.environ["CTXGATE_TRIM_TARGET_FLOOR"] = "20000"
os.environ["CTXGATE_DB_DSN"] = "postgresql://localhost:5432/ctxproxy"
os.environ["CTXGATE_VLLM_URL"] = "http://127.0.0.1:29000/v1"

import proxy.app as app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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


def _make_pair(i):
    """One assistant+tool pair, each ~800 tokens."""
    return [
        {
            "role": "assistant",
            "content": "assistant %d " % i + _pad(780),
            "tool_calls": [
                {
                    "id": "tc%d" % i,
                    "type": "function",
                    "function": {
                        "name": "shell",
                        "arguments": json.dumps({"cmd": "echo %d" % i}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "tc%d" % i,
            "content": "tool out %d " % i + _pad(780),
        },
    ]


def _make_history(n_pairs):
    """3-message seed + n_pairs assistant/tool pairs (~800 tok each)."""
    msgs = [
        {"role": "system", "content": "system seed " + _pad(780)},
        {"role": "user", "content": "user seed " + _pad(780)},
        {"role": "assistant", "content": "assistant seed " + _pad(780)},
    ]
    for i in range(n_pairs):
        msgs.extend(_make_pair(i))
    return msgs


def _reset():
    app.session_compactions.clear()
    app._window_locks.clear()
    app._window_load_tried.clear()
    app.session_prefix_hashes.clear()
    app.metrics["emergency_shrink_total"] = 0
    app.metrics["emergency_shrink_groups_dropped"] = 0
    app.metrics["trim_sticky_reuse"] = 0
    app.metrics["trim_events"] = 0
    app.metrics["compaction_events"] = 0
    app.metrics["dropped_total"] = 0
    app._window_persist_enabled = None
    app._window_persist_warned = False
    if hasattr(app, "_window_persist_cooldown_until"):
        app._window_persist_cooldown_until = 0.0


SK = "sticky-test:00000000"
CEILING = min(app.MAX_INPUT, app.MAX_CONTEXT - app.SAFETY_MARGIN - app.MIN_OUTPUT)


# ---------------------------------------------------------------------------
# Test 1: Failing-first regression
# ---------------------------------------------------------------------------

def test_sticky_reroll_recuts_not_shrinks():
    """Preload a valid ws at cut=17 with a 203-msg history (~162k tok).
    build_context must re-cut (slow path), NOT emergency-shrink.
    After: result <= ceiling, cut > 17, emergency_shrink_total unchanged (0).
    """
    _reset()
    history = _make_history(100)  # 3 seed + 200 msgs = 203 total
    seed_raw = history[:3]
    rest_raw = history[3:]

    # Preload a VALID ws at cut=17
    ws = app._new_window_state(17, rest_raw, seed_raw, 0)
    app.session_compactions[SK] = ws

    es_before = app.metrics["emergency_shrink_total"]

    result = _run(app.build_context(history, task_uuid=None, session_key=SK))

    result_tok = app.count_messages_tokens(result)
    new_ws = app.session_compactions.get(SK)

    # Result must be under the ceiling
    assert result_tok <= CEILING, f"result {result_tok} > ceiling {CEILING}"
    # The cut must have advanced past 17 (re-cut, not stuck)
    assert new_ws is not None, "session_compactions[SK] missing after build_context"
    assert new_ws["cut"] > 17, f"cut stuck at {new_ws['cut']} (expected > 17)"
    # emergency_shrink must NOT have been called
    assert app.metrics["emergency_shrink_total"] == es_before, (
        f"emergency_shrink_total changed: {es_before} -> {app.metrics['emergency_shrink_total']}"
    )


# ---------------------------------------------------------------------------
# Test 2: Prefix-stability simulation
# ---------------------------------------------------------------------------

def test_sticky_prefix_stability():
    """From the re-cut state, append 2 msgs/step for 30 steps.
    (a) cut changes <= 4
    (b) on no-cut-change steps, prev kept is exact prefix of new kept
    (c) emergency_shrink_total never increments
    """
    _reset()
    history = _make_history(100)  # start with 203 msgs
    seed_raw = history[:3]
    rest_raw = history[3:]

    # Initial re-cut (same setup as test 1)
    ws = app._new_window_state(17, rest_raw, seed_raw, 0)
    app.session_compactions[SK] = ws

    es_before = app.metrics["emergency_shrink_total"]
    state = {"prev_kept": None, "cut_changes": 0, "prev_cut": 17}

    async def _simulate():
        # Step 0: initial build (triggers the re-cut from 17)
        result = await app.build_context(list(history), task_uuid=None, session_key=SK)
        ws_now = app.session_compactions[SK]
        if ws_now["cut"] != state["prev_cut"]:
            state["cut_changes"] += 1
            state["prev_cut"] = ws_now["cut"]
        state["prev_kept"] = list(result)

        # Steps 1-30: append 2 msgs each
        pair_idx = 100
        for step in range(1, 31):
            history.extend(_make_pair(pair_idx))
            pair_idx += 1

            result = await app.build_context(list(history), task_uuid=None, session_key=SK)
            ws_now = app.session_compactions[SK]
            new_cut = ws_now["cut"]

            if new_cut != state["prev_cut"]:
                state["cut_changes"] += 1
                state["prev_cut"] = new_cut
            else:
                # Cut unchanged: prev kept must be exact prefix of new kept
                n = min(len(state["prev_kept"]), len(result))
                for i in range(n):
                    assert state["prev_kept"][i] == result[i], (
                        f"step {step}: prefix mismatch at idx {i} "
                        f"(cut unchanged at {new_cut})"
                    )
            state["prev_kept"] = list(result)

    _run(_simulate())

    # (c) emergency_shrink never incremented
    assert app.metrics["emergency_shrink_total"] == es_before, (
        f"emergency_shrink_total incremented: {es_before} -> {app.metrics['emergency_shrink_total']}"
    )
    # (a) cut changes <= 4
    assert state["cut_changes"] <= 4, f"too many cut changes: {state['cut_changes']} (max 4)"
    # Sanity: at least one re-cut happened (the initial one)
    assert state["cut_changes"] >= 1, "no re-cut happened at all"

#!/usr/bin/env python3
"""harness_window.py - Phase 0 prefix-stability harness.

Imports the dev app module WITHOUT starting servers (pool stays None). Replays a
message history through build_context request-by-request (history grows 1-2 msgs
per request, exactly like Goose resending the full history) and records, per
request: kept token count, SHA of every sent message, and the index of the first
message that differs vs the previous request.

Usage:
  python3 harness_window.py --out reference_prefix.json            # synthetic history
  python3 harness_window.py --history real.json --out real_prefix.json

No vLLM calls, no DB (pool=None), no real Mistral (task_uuid=None so the
summarizer is never spawned). Deterministic: same input -> same output.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_DIR = os.path.join(os.path.dirname(HERE), "proxy")
sys.path.insert(0, PROXY_DIR)

import app  # noqa: E402  (module import only; __main__ server guard not hit)


def _msg_hash(m):
    """Stable per-message fingerprint (same scheme as _prefix_diag)."""
    c = m.get("content", "")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    tc = m.get("tool_calls")
    tc_s = json.dumps(tc, sort_keys=True) if tc else ""
    return hashlib.sha1((str(m.get("role", "")) + "|" + str(c) + "|" + tc_s).encode()).hexdigest()


def _tool_call_msg(name, args, call_id, result_text):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }, {"role": "tool", "tool_call_id": call_id, "content": result_text}


def synthetic_history():
    """A realistic growing history: seed(3) + tool loops + new user messages.

    Designed to (a) exceed MAX_INPUT to trigger the slow path + re-cut,
    (b) exercise the sticky fast path across many turns, (c) create a new
    epoch via a second user message, (d) include Polish diacritics.
    """
    h = []
    # --- seed (first 3 messages) ---
    h.append({"role": "system", "content": "You are a senior engineer. Follow the plan. " * 40})
    h.append({"role": "user", "content": "Build the CV parser pipeline. Start with the matcher flow and write a DFMEA."})
    # seed assistant carries a tool_call whose result is message index 3 (outside seed) -> F8 dangling
    h.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "chatcmpl-tool-SEED0001",
            "type": "function",
            "function": {"name": "text_editor", "arguments": json.dumps({"command": "write", "path": "/home/pawelw/goose-scratch/cv-fmea-20261007.md", "text": "DFMEA draft"})},
        }],
    })
    # index 3: the tool result for the seed call
    h.append({"role": "tool", "tool_call_id": "chatcmpl-tool-SEED0001", "content": "Wrote /home/pawelw/goose-scratch/cv-fmea-20261007.md (DFMEA draft, 2400 chars)"})
    # --- tool loop: many assistant/tool pairs (grows history, forces re-cut) ---
    files = [
        "cv-implementation-plan-20261007.md",
        "pii-residual-scan-20261007.md",
        "NOTES.md",
        "cv-architecture-master-20261006.md",
        "cvparser-flow-20261006.md",
        "matcher-flow-20261006.md",
        "bff-frontend-flow-20261006.md",
    ]
    i = 0
    for k in range(60):
        i += 1
        if k % 8 == 0 and k > 0:
            # periodic assistant prose (decisions)
            h.append({"role": "assistant", "content": "Decision %d: " % k + "refine the matcher scoring and update the FMEA. " * 20})
        fname = files[k % len(files)]
        a, t = _tool_call_msg(
            "text_editor",
            {"command": "str_replace", "path": "/home/pawelw/goose-scratch/" + fname, "text": "section %d" % k},
            "chatcmpl-tool-%04d" % i,
            "Updated /home/pawelw/goose-scratch/" + fname + " (section %d). " % k + "x" * 4000,
        )
        h.append(a)
        h.append(t)
        # occasional shell test run
        if k % 10 == 5:
            i += 1
            a2, t2 = _tool_call_msg(
                "shell",
                {"command": "mvn test -q"},
                "chatcmpl-tool-%04d" % i,
                "Tests run: %d, Failures: 0, Errors: 0, Skipped: 0. BUILD SUCCESS" % (500 + k),
            )
            h.append(a2)
            h.append(t2)
    # --- NEW user message (new epoch; toggles pinned copy at index 4) ---
    h.append({"role": "user", "content": "Teraz podsumuj sesje: co zrobilismy? Zażółć gęślą jaźń. What did we do so far?"})
    h.append({"role": "assistant", "content": "Here is a recap of the session deliverables. " * 30})
    # a few more tool turns after the new user msg
    for k in range(6, 12):
        i += 1
        a, t = _tool_call_msg(
            "text_editor",
            {"command": "write", "path": "/home/pawelw/goose-scratch/final-%d.md" % k, "text": "final"},
            "chatcmpl-tool-%04d" % i,
            "Wrote /home/pawelw/goose-scratch/final-%d.md" % k,
        )
        h.append(a)
        h.append(t)
    return h


def load_history(path):
    with open(path) as f:
        return json.load(f)


async def replay(history, session_key):
    """Feed history to build_context request-by-request; record prefix stability."""
    requests = []
    prev_hashes = None
    n = len(history)
    # grow by 2 messages per request (1 assistant + 1 tool), like a live agent loop
    step = 2
    for start in range(0, n, step):
        chunk = history[: start + step]
        kept = await app.build_context(chunk, task_uuid=None, session_key=session_key)
        hashes = [_msg_hash(m) for m in kept]
        first_diff = None
        if prev_hashes is not None:
            for idx in range(min(len(prev_hashes), len(hashes))):
                if prev_hashes[idx] != hashes[idx]:
                    first_diff = idx
                    break
            if first_diff is None and len(hashes) != len(prev_hashes):
                first_diff = min(len(prev_hashes), len(hashes))  # pure append
        kept_tok = app.count_messages_tokens(kept)
        requests.append({
            "req": len(requests),
            "history_len": len(chunk),
            "kept_msgs": len(kept),
            "kept_tokens": kept_tok,
            "first_diff": first_diff,
            "hashes": hashes,
        })
        prev_hashes = hashes
    return requests


def summarize(requests):
    """Collapse per-request hashes out of the summary (keep first_diff sequence)."""
    out = []
    ratios = []
    for r in requests:
        out.append({
            "req": r["req"],
            "history_len": r["history_len"],
            "kept_msgs": r["kept_msgs"],
            "kept_tokens": r["kept_tokens"],
            "first_diff": r["first_diff"],
        })
        if r["first_diff"] is not None:
            ratios.append(r["first_diff"] / max(1, r["kept_msgs"]))
    mean_ratio = sum(ratios) / len(ratios) if ratios else None
    return {
        "n_requests": len(out),
        "mean_stable_ratio": mean_ratio,
        "requests": out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=None, help="JSON file with a message list; omit for synthetic")
    ap.add_argument("--out", default=os.path.join(HERE, "reference_prefix.json"))
    ap.add_argument("--session-key", default="harness:synthetic")
    args = ap.parse_args()
    history = load_history(args.history) if args.history else synthetic_history()
    # Reset in-memory window state so each run is independent/deterministic.
    app.session_compactions.clear()
    app.session_prefix_hashes.clear()
    app.session_fingerprints.clear()
    requests = asyncio.run(replay(history, args.session_key))
    summary = summarize(requests)
    # Store full hashes in the file (for G-PREFIX diffing) but keep summary lean.
    payload = {
        "session_key": args.session_key,
        "history_len": len(history),
        "summary": summary,
        "requests_full": requests,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f)
    # Print a compact report to stdout.
    print("history_len=%d requests=%d mean_stable_ratio=%s" % (
        len(history), summary["n_requests"],
        ("%.4f" % summary["mean_stable_ratio"]) if summary["mean_stable_ratio"] is not None else "n/a"))
    diffs = [r["first_diff"] for r in summary["requests"]]
    print("first_diff sequence:", diffs)
    print("wrote", args.out)


if __name__ == "__main__":
    main()

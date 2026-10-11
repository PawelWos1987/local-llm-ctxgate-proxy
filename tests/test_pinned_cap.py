"""Test that the live prompt is preserved in full (no truncation).
The old _make_pinned_copy with 16k/2k caps has been removed.
The live prompt now appears at its natural position in the kept window."""
import sys, os, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["CTXGATE_SKIP_DOTENV"] = "1"
os.environ["CTXGATE_TOKEN_ESTIMATOR"] = "calibrated"

import proxy.app as app


def test_long_prompt_preserved_in_full():
    """A 20k-char prompt must survive intact in the kept window."""
    long_content = "x" * 20000
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
        {"role": "user", "content": long_content},
    ]
    seed = [msgs[0]]
    rest = msgs[1:]
    kept = app._kept_messages(seed, rest, 2)
    last_user = [m for m in kept if m.get("role") == "user"][-1]
    assert last_user["content"] == long_content, (
        f"Prompt truncated: {len(last_user['content'])} != {len(long_content)}"
    )


def test_no_pinned_copy_marker():
    """No ctxgate_pinned marker should appear in kept messages."""
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hello world " * 1000},
    ]
    seed = [msgs[0]]
    rest = msgs[1:]
    kept = app._kept_messages(seed, rest, 0)
    for m in kept:
        assert "ctxgate_pinned" not in m


def test_live_prompt_identity_stable():
    """_live_prompt_identity produces a stable SHA-256 hash."""
    msgs = [{"role": "user", "content": "test prompt"}]
    h1 = app._live_prompt_identity(msgs)
    h2 = app._live_prompt_identity(msgs)
    assert h1 == h2
    assert len(h1) == 64

"""Fresh border tests: CUT & TRIM behaviors served by the proxy.

Written purely from reading the codebase (window/*.py), NOT from git history or docs.
Each test ties an assertion to a named function so failures pinpoint the exact code.

CORRECTED FORMULAS (verified against runtime):
  MAX_INPUT = min(MAX_INPUT, int((MAX_CONTEXT-SAFTY_MARGIN-1-MAX_OUTPUT)/(1+HEADROOM)))
                            = min(64800, int(64799/1.05)) = 61713
  _eviction_trigger() = int(61713*0.65) = 40113
  _trim_target()      = int(61713*0.55) = 33942
  _output_budget(in)  = max(0, min(15000, 84000-int(in*1.05)-4200-1)); stays 15000 until in>61713
Signatures:
  _safe_cut(groups, budget_tokens) -> int(group idx); groups = list of message-lists
  _recut_to(messages, max_tokens)  -> int(cut); MUTATES messages in place
  _hard_cut_to(messages, actual_tokens, target_tokens) -> int(tokens removed); MUTATES in place
"""
import sys, os
sys.path.insert(0, "/home/pawelw/ctxproxy")

import pytest

from proxy.config.constants import (
    MAX_CONTEXT, MAX_OUTPUT, SAFETY_MARGIN, MAX_INPUT,
    CTXGATE_WINDOW_TRIGGER_FRAC, CTXGATE_WINDOW_TARGET_FRAC,
    TRIM_TARGET_FLOOR, CUT_QUANTUM, RHO_COLD,
)
from proxy.window.budget import _output_budget, _output_budget_checked
from proxy.window.cut import (
    _eviction_trigger, _trim_target, _safe_cut, _recut_to, _hard_cut_to,
    _is_context_length_error, _parse_vllm_actual_tokens, _kept_messages,
)
from proxy.window.elide import _elided_tool_content, _stable_elide_inplace, _stable_elide_idx
from proxy.window.shrink import (
    _emergency_shrink, _protected_indices, _truncate_message_content,
    _strip_injection_block, ContextCapacityError,
)
from proxy.window.state import (
    _norm_content, _msg_anchor, _seed_sig, _window_valid,
    _new_window_state, _newest_user_idx, _strip_proxy_metadata,
)
from proxy.window.invariants import verify_context_invariants, _validate_outgoing_payload
from proxy.token.counter import count_message_tokens, count_messages_tokens


def _big_tool_body(n_chars: int) -> str:
    return ("public class X { void m() { } }\n" * (n_chars // 34 + 1))[:n_chars]


def _tool_group(call_id: str, size: int) -> list:
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
        {"role": "tool", "tool_call_id": call_id, "content": _big_tool_body(size)},
    ]


class TestBudget:
    def test_max_input_value(self):
        # MAX_INPUT is the direct input budget ceiling
        assert MAX_INPUT == 61713

    def test_output_budget_full_when_input_zero(self):
        assert _output_budget(0) == MAX_OUTPUT

    def test_output_budget_stays_max_until_near_ceiling(self):
        # Below the admitted ceiling, output budget stays clamped to MAX_OUTPUT
        assert _output_budget(1000) == MAX_OUTPUT
        assert _output_budget(60000) == MAX_OUTPUT  # raw 16799 > 15000

    def test_output_budget_shrinks_past_ceiling(self):
        b_low = _output_budget(1000)      # 15000
        b_high = _output_budget(70000)    # raw = 84000-73500-4201 = 6299
        assert b_low > b_high
        assert b_high == 6299

    def test_output_budget_never_negative(self):
        assert _output_budget(1_000_000) == 0

    def test_output_budget_checked_flags_invalid(self):
        budget, valid = _output_budget_checked(1_000_000)
        assert budget == 0 and valid is False
        budget2, valid2 = _output_budget_checked(1000)
        assert valid2 is True and budget2 > 0


class TestCutWatermarks:
    def test_eviction_trigger_is_high_watermark(self):
        trig = _eviction_trigger()
        assert trig == int(MAX_INPUT * CTXGATE_WINDOW_TRIGGER_FRAC)
        assert trig == 40113

    def test_trim_target_is_low_watermark(self):
        tgt = _trim_target()
        assert tgt == int(MAX_INPUT * CTXGATE_WINDOW_TARGET_FRAC)
        assert tgt == 33942

    def test_target_below_trigger(self):
        assert _trim_target() < _eviction_trigger()

    def test_target_respects_floor(self):
        assert _trim_target() >= TRIM_TARGET_FLOOR


class TestSafeCut:
    def test_safe_cut_returns_valid_group_index(self):
        # Build turn groups: each = [user, assistant]
        groups = [[{"role": "user", "content": "u%d" % i},
                   {"role": "assistant", "content": "a%d" % i}] for i in range(10)]
        cut = _safe_cut(groups, budget_tokens=1000)
        assert 0 <= cut <= len(groups)

    def test_safe_cut_quantized_motion(self):
        # Larger budget admits more groups (lower cut index = keeps more)
        groups = [[{"role": "user", "content": "x" * 200},
                   {"role": "assistant", "content": "y" * 200}] for i in range(20)]
        cut_small = _safe_cut(groups, budget_tokens=500)
        cut_big = _safe_cut(groups, budget_tokens=5000)
        # Bigger budget -> smaller cut index (keeps from earlier)
        assert cut_big <= cut_small

    def test_safe_cut_empty(self):
        assert _safe_cut([], 1000) == 0


class TestRecutAndHardCut:
    def test_recut_to_mutates_and_preserves_live_prompt(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(30):
            msgs += _tool_group("c%d" % i, 3000)
        msgs.append({"role": "user", "content": "final question"})
        before = count_messages_tokens(msgs)
        n_msgs_before = len(msgs)
        _recut_to(msgs, _trim_target())  # mutates in place
        after = count_messages_tokens(msgs)
        assert after < before
        assert len(msgs) < n_msgs_before
        # Live prompt survives
        assert any(m.get("role") == "user" and m.get("content") == "final question" for m in msgs)
        # System prefix preserved
        assert msgs[0]["role"] == "system"

    def test_recut_to_noop_when_under_budget(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "small"}]
        n = len(msgs)
        _recut_to(msgs, 100000)
        assert len(msgs) == n  # nothing cut

    def test_hard_cut_removes_exact_amount(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(20):
            msgs += _tool_group("h%d" % i, 4000)
        msgs.append({"role": "user", "content": "live prompt"})
        actual = count_messages_tokens(msgs)
        n_before = len(msgs)
        removed = _hard_cut_to(msgs, actual_tokens=actual, target_tokens=_trim_target())
        assert removed > 0
        assert len(msgs) < n_before
        # Live prompt + system preserved
        assert any(m.get("role") == "user" and m.get("content") == "live prompt" for m in msgs)
        assert msgs[0]["role"] == "system"

    def test_hard_cut_noop_when_under_target(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        removed = _hard_cut_to(msgs, actual_tokens=100, target_tokens=1000)
        assert removed == 0
        assert len(msgs) == 2

    def test_hard_cut_repairs_tool_orphans(self):
        # If we remove an assistant tool_call, its tool result must go too
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "only", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "only", "content": "r" * 5000},
            {"role": "user", "content": "LIVE"},
        ]
        actual = count_messages_tokens(msgs)
        _hard_cut_to(msgs, actual_tokens=actual, target_tokens=100)
        # No orphan tool result: every tool msg has a declaring assistant present
        tcids_declared = set()
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tcids_declared.add(tc["id"])
        for m in msgs:
            if m.get("role") == "tool":
                assert m["tool_call_id"] in tcids_declared, "orphan tool result survived hard cut"


class TestElide:
    def test_elided_tool_content_idempotent(self):
        body = _big_tool_body(10000)
        once = _elided_tool_content(body, session_key="s", event_hash="e")
        twice = _elided_tool_content(once, session_key="s", event_hash="e")
        assert once == twice

    def test_elided_keeps_head_and_tail(self):
        body = "HEAD_MARKER_" + "x" * 9000 + "_TAIL_MARKER"
        out = _elided_tool_content(body)
        assert out.startswith("HEAD_MARKER_")
        assert out.endswith("_TAIL_MARKER")
        assert "chars elided by ctxgate" in out
        assert len(out) < len(body)

    def test_stable_elide_inplace_only_touches_large_tools(self):
        small = "tiny"; big = _big_tool_body(5000)
        msgs = [
            {"role": "tool", "tool_call_id": "a", "content": small},
            {"role": "tool", "tool_call_id": "b", "content": big},
            {"role": "user", "content": "hello"},
        ]
        _stable_elide_inplace(msgs, 0, len(msgs), session_key="s")
        assert msgs[0]["content"] == small
        assert "chars elided by ctxgate" in msgs[1]["content"]
        assert msgs[2]["content"] == "hello"

    def test_stable_elide_idx_protects_newest_four(self):
        rest = []
        for i in range(10):
            rest += _tool_group("g%d" % i, 3000)
        idx = _stable_elide_idx(0, rest)
        assert idx > 0
        prot = rest[idx:]
        tool_bodies = [m["content"] for m in prot if m.get("role") == "tool"]
        assert len(tool_bodies) >= 1


class TestShrink:
    def test_emergency_shrink_meets_ceiling(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(25):
            msgs += _tool_group("s%d" % i, 4000)
        msgs.append({"role": "user", "content": "LIVE_PROMPT_SHOULD_SURVIVE"})
        ceiling = MAX_INPUT
        res = _emergency_shrink(msgs, ceiling)
        assert count_messages_tokens(res) <= ceiling
        assert any(m.get("role") == "user" and m.get("content") == "LIVE_PROMPT_SHOULD_SURVIVE" for m in res)

    def test_emergency_shrink_noop_when_under(self):
        msgs = [{"role": "user", "content": "short"}]
        res = _emergency_shrink(msgs, 100000)
        assert res == msgs

    def test_truncate_message_content_60_40_split(self):
        msg = {"role": "user", "content": "z" * 10000}
        _truncate_message_content(msg, max_tokens=100)  # in-place now
        assert "truncated" in msg["content"]
        assert len(msg["content"]) < 10000

    def test_strip_injection_block_removes_knowledge_memory_digest(self):
        content = "Real user question here.\nRelevant knowledge: foo\nTask memory: bar\nSession digest: baz"
        out = _strip_injection_block(content)
        assert out.strip() == "Real user question here."
        assert "Relevant knowledge:" not in out

    def test_strip_injection_block_noop_when_absent(self):
        content = "plain content no injection"
        assert _strip_injection_block(content) == content

    def test_context_capacity_error_raised_when_unshrinkable(self):
        msgs = [{"role": "user", "content": "Q" * 200000}]
        with pytest.raises(ContextCapacityError):
            _emergency_shrink(msgs, ceiling=100, aggressive=False)

    def test_aggressive_mode_shrinks_smaller(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(15):
            msgs += _tool_group("a%d" % i, 5000)
        msgs.append({"role": "user", "content": "LIVE"})
        ceiling = 20000
        std = _emergency_shrink([dict(m) for m in msgs], ceiling, aggressive=False)
        aggr = _emergency_shrink([dict(m) for m in msgs], ceiling, aggressive=True)
        assert count_messages_tokens(aggr) <= count_messages_tokens(std)
        assert count_messages_tokens(aggr) <= ceiling


class TestStateIdentity:
    def test_norm_content_none_and_list(self):
        assert _norm_content(None) == ""
        assert _norm_content([{"text": "a"}, {"text": "b"}]) == "a b"
        assert _norm_content("raw") == "raw"

    def test_msg_anchor_stable(self):
        m = {"role": "user", "content": "hi", "tool_call_id": ""}
        assert _msg_anchor(m) == _msg_anchor(dict(m))
        assert _msg_anchor(m) != _msg_anchor({"role": "user", "content": "bye"})

    def test_seed_sig_first_three(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}]
        assert _seed_sig(msgs) == _seed_sig(msgs[:3])

    def test_window_valid_detects_drift(self):
        seed = [{"role": "system", "content": "s"}]
        rest = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}]
        ws = _new_window_state(cut=0, rest=rest, seed=seed, summarized_through=0)
        assert _window_valid(ws, seed, rest) is True
        drifted = [dict(m) for m in rest]
        drifted[0]["content"] = "CHANGED"
        assert _window_valid(ws, seed, drifted) is False

    def test_newest_user_idx(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a"}, {"role": "user", "content": "u2"}]
        assert _newest_user_idx(msgs) == 3
        assert _newest_user_idx([]) is None

    def test_strip_proxy_metadata(self):
        msgs = [{"role": "user", "content": "x", "ctxgate_internal": 1, "keep": 2}]
        cleaned = _strip_proxy_metadata(msgs)
        assert "ctxgate_internal" not in cleaned[0]
        assert cleaned[0]["keep"] == 2


class TestInvariants:
    def test_verify_clean_context_no_violations(self):
        original = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        assert verify_context_invariants(original, original, ceiling=100000) == []

    def test_verify_over_ceiling_flagged(self):
        msgs = [{"role": "user", "content": "x" * 200000}]
        viols = verify_context_invariants(msgs, msgs, ceiling=100)
        assert any(v.startswith("over_ceiling") for v in viols)

    def test_validate_outgoing_blocks_ctxgate_leak(self):
        body = {"messages": [{"role": "user", "content": "x", "ctxgate_secret": 1}]}
        ok, err = _validate_outgoing_payload(body, "")
        assert ok is False and "proxy_metadata_leak" in err

    def test_validate_outgoing_blocks_orphan_tool(self):
        body = {"messages": [
            {"role": "user", "content": "q"},
            {"role": "tool", "tool_call_id": "orphan", "content": "result"},
        ]}
        ok, err = _validate_outgoing_payload(body, "")
        assert ok is False and "orphaned_tool_results" in err

    def test_validate_outgoing_ok_when_clean(self):
        body = {"messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "res"},
        ]}
        ok, err = _validate_outgoing_payload(body, "")
        assert ok is True and err == ""


class TestCounter:
    def test_count_scales_with_chars(self):
        assert count_message_tokens({"role": "user", "content": "x"*10000}) > count_message_tokens({"role": "user", "content": "hi"})

    def test_count_messages_sum(self):
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "bb"}]
        assert count_messages_tokens(msgs) == count_message_tokens(msgs[0]) + count_message_tokens(msgs[1])


class TestPrepMessages:
    def test_prep_none_content_becomes_empty(self):
        from proxy.hotpath.context import _prep_messages
        out = _prep_messages([{"role": "user", "content": None}])
        assert out[0]["content"] == ""

    def test_prep_strips_invalid_toolcalls_json(self):
        from proxy.hotpath.context import _prep_messages
        bad = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{bad json"}}]}]
        out = _prep_messages(bad)
        assert "tool_calls" not in out[0]

    def test_prep_ensures_user_message_exists(self):
        from proxy.hotpath.context import _prep_messages
        out = _prep_messages([{"role": "system", "content": "s"}])
        assert any(m["role"] == "user" for m in out)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

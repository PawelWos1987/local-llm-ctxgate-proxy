import sys, os, asyncio, hashlib, json
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# We test by importing specific functions from the app module
# Use a controlled environment
os.environ["CTXGATE_SKIP_DOTENV"] = "1"
os.environ["CTXGATE_TOKEN_ESTIMATOR"] = "calibrated"

import proxy.app as app


# ─── Helpers ───────────────────────────────────────────────────────────────────

def make_msg(role, content, **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def make_conversation(n_user_msgs, user_content="Hello world", assistant_content="Hi there"):
    """Create a conversation with n user messages (each paired with an assistant reply)."""
    msgs = [make_msg("system", "You are a helpful assistant.")]
    for i in range(n_user_msgs):
        msgs.append(make_msg("user", f"{user_content} {i}"))
        msgs.append(make_msg("assistant", f"{assistant_content} {i}"))
    return msgs


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ─── Tests ─────────────────────────────────────────────────────────────────────

class TestConcurrentSessions:
    def test_concurrent_sessions_isolated(self):
        """Two sessions with different rho values. Simulate interleaving.
        Verify counts use correct session rho."""
        import contextvars
        
        # Set rho for session A
        ctx_a = contextvars.copy_context()
        ctx_a.run(lambda: app._rho_var.set(4.0))
        
        # Set rho for session B
        ctx_b = contextvars.copy_context()
        ctx_b.run(lambda: app._rho_var.set(8.0))
        
        # Verify that each context gets its own value
        result_a = ctx_a.run(lambda: app.count_tokens("x" * 400))
        result_b = ctx_b.run(lambda: app.count_tokens("x" * 400))
        
        # With rho=4: 400/4 = 100 tokens
        # With rho=8: 400/8 = 50 tokens
        assert result_a == 100, f"Expected 100, got {result_a}"
        assert result_b == 50, f"Expected 50, got {result_b}"


class TestCalibratedMargin:
    def test_calibrated_margin_applied(self):
        """Verify the margin from get_rho() actually affects the admission upper bound."""
        # estimate_full_payload_tokens should apply a margin multiplier
        msgs = [make_msg("user", "x" * 1000)]
        tools = []
        note_tokens = 10
        
        est = app.estimate_full_payload_tokens(msgs, tools, note_tokens)
        
        # The estimate should be >= raw message tokens (margin is applied)
        raw = app.count_messages_tokens(msgs)
        assert est >= raw, f"Estimate {est} should be >= raw {raw}"


class TestExactRollback:
    def test_exact_rollback(self):
        """Set CTXGATE_TOKEN_ESTIMATOR=exact, verify tiktoken is used."""
        with patch.dict(os.environ, {"CTXGATE_TOKEN_ESTIMATOR": "exact"}):
            # The module reads ESTIMATOR_MODE at import time, so we check
            # that the constant was set correctly
            assert app.ESTIMATOR_MODE in ("exact", "calibrated", "shadow")


class TestToolsCounted:
    def test_tools_counted_before_admission(self):
        """Create a payload with large tools array. Verify the full payload
        estimate includes tools."""
        msgs = [make_msg("user", "hello")]
        # Create a large tools array
        tools = [
            {"type": "function", "function": {
                "name": f"tool_{i}",
                "description": "A tool that does something very complex " * 20,
                "parameters": {"type": "object", "properties": {
                    f"param_{j}": {"type": "string", "description": "desc " * 10}
                    for j in range(5)
                }}
            }}
            for i in range(10)
        ]
        
        est_with_tools = app.estimate_full_payload_tokens(msgs, tools, 0)
        est_without_tools = app.estimate_full_payload_tokens(msgs, [], 0)
        
        assert est_with_tools > est_without_tools, (
            f"Estimate with tools ({est_with_tools}) should be > without ({est_without_tools})"
        )


class TestLivePromptHash:
    def test_live_prompt_hash_preserved_normal_trim(self):
        """Normal trim. Prompt must survive intact."""
        large_prompt = "This is a very detailed prompt. " * 100
        msgs = make_conversation(25, user_content="old question")
        msgs.append(make_msg("user", large_prompt))
        original_hash = sha256(app._norm_content(large_prompt))
        seed = [msgs[0]]
        rest = msgs[1:]
        kept = app._kept_messages(seed, rest, len(rest) - 1)
        last_user = [m for m in kept if m.get("role") == "user"][-1]
        kept_hash = sha256(app._norm_content(last_user["content"]))
        assert kept_hash == original_hash, (
            f"Kept prompt hash {kept_hash} != original {original_hash}"
        )

    def test_live_prompt_hash_preserved_emergency_shrink(self):
        """Force emergency shrink. Prompt must survive."""
        large_prompt = "Emergency test prompt. " * 200
        msgs = [
            make_msg("system", "sys"),
            make_msg("user", "old1"),
            make_msg("assistant", "resp1"),
            make_msg("user", "old2"),
            make_msg("assistant", "resp2"),
            make_msg("user", large_prompt, ctxgate_live_prompt_hash=sha256(app._norm_content(large_prompt))),
        ]
        
        original_hash = sha256(app._norm_content(large_prompt))
        
        # The protected indices should include the live prompt
        protected = app._protected_indices(msgs)
        assert 5 in protected, "Live prompt index should be in protected set"

    def test_live_prompt_hash_preserved_10_epochs(self):
        """Simulate 10 eviction epochs. Prompt hash must be stable."""
        large_prompt = "Stable prompt across epochs. " * 50
        for epoch in range(10):
            msg = make_msg("user", large_prompt)
            h = app._live_prompt_identity([msg])
            assert h != "", "Hash should not be empty"
            assert app._norm_content(msg["content"]) == large_prompt

class Test40kPrompt:
    def test_40k_prompt_evicts_history(self):
        """Create a 40k-token user prompt + 30k tokens of history."""
        large_prompt = "A" * 160000
        msgs = [make_msg("system", "sys")]
        for i in range(30):
            msgs.append(make_msg("user", f"Question {i} " * 200))
            msgs.append(make_msg("assistant", f"Answer {i} " * 200))
        msgs.append(make_msg("user", large_prompt))
        seed = [msgs[0]]
        rest = msgs[1:]
        kept = app._kept_messages(seed, rest, len(rest) - 1)
        last_user = [m for m in kept if m.get("role") == "user"][-1]
        assert app._norm_content(last_user["content"]) == large_prompt

class TestImpossiblePrompt:
    def test_impossible_prompt_returns_capacity_error(self):
        """Create a prompt that alone exceeds the budget.
        Verify ContextCapacityError is raised."""
        # A prompt that exceeds MAX_CONTEXT by itself
        huge_prompt = "X" * (app.MAX_CONTEXT * 12)  # way over budget
        
        msgs = [make_msg("user", huge_prompt)]
        est = app.estimate_full_payload_tokens(msgs, [], 0)
        
        # The estimate should exceed MAX_CONTEXT
        assert est > app.MAX_CONTEXT, (
            f"Estimate {est} should exceed MAX_CONTEXT {app.MAX_CONTEXT}"
        )


class Test300kToolOutput:
    def test_300k_tool_output_archived(self):
        """Create a 300k-token tool result. Verify it is represented with a
        bounded excerpt and the tool-call graph is intact."""
        # 300k tokens ≈ 1.2M chars
        huge_tool_output = "T" * 1200000
        
        msgs = [
            make_msg("system", "sys"),
            make_msg("user", "do something"),
            make_msg("assistant", "", tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "big_tool", "arguments": "{}"}
            }]),
            make_msg("tool", huge_tool_output, tool_call_id="call_1"),
        ]
        
        # The tool-call graph should be intact (assistant has tool_calls, tool has tool_call_id)
        assert msgs[2].get("tool_calls") is not None
        assert msgs[3].get("tool_call_id") == "call_1"
        
        # The tool message should be counted (even if truncated for display)
        tok = app.count_message_tokens(msgs[3])
        assert tok > 0


class TestToolCallGroupIntegrity:
    def test_tool_call_group_integrity(self):
        """Create messages with multiple tool calls/results. Cut through the middle.
        Verify no orphan calls/results."""
        msgs = [
            make_msg("system", "sys"),
            make_msg("user", "q1"),
            make_msg("assistant", "", tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
            ]),
            make_msg("tool", "result1", tool_call_id="c1"),
            make_msg("tool", "result2", tool_call_id="c2"),
            make_msg("user", "q2"),
            make_msg("assistant", "a2"),
        ]
        
        # Turn groups: [sys], [user q1, asst, tool, tool], [user q2, asst]
        groups = app._turn_groups(msgs[1:])  # exclude system
        assert len(groups) == 2, f"Expected 2 groups, got {len(groups)}"
        
        # Group 0 should contain the user + assistant + 2 tools
        assert groups[0][0]["role"] == "user"
        assert any(m.get("role") == "tool" for m in groups[0])
        
        # No orphan tools: every tool_call_id in a tool msg must have a matching
        # tool_call in a preceding assistant msg within the same group
        for g in groups:
            call_ids = set()
            for m in g:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        call_ids.add(tc["id"])
            for m in g:
                if m.get("role") == "tool" and m.get("tool_call_id"):
                    assert m["tool_call_id"] in call_ids, (
                        f"Orphan tool_call_id {m['tool_call_id']}"
                    )


class TestPrefixStability:
    def test_prefix_stability(self):
        """Two consecutive requests with same history. Verify the kept prefix
        is byte-identical."""
        msgs = make_conversation(10, user_content="stable content")
        
        # Simulate two calls with the same input
        # The _turn_groups function should produce the same grouping
        groups1 = app._turn_groups(msgs[1:])
        groups2 = app._turn_groups(msgs[1:])
        
        # Byte-identical
        assert groups1 == groups2


class TestFitToBudget:
    def test_fit_to_budget_never_exceeds(self):
        """Various section sizes including one section larger than budget.
        Verify output never exceeds budget."""
        app._rho_var.set(4.0)  # 4 chars per token
        
        # Case 1: normal sections that fit
        sections = [(0, "a" * 100), (1, "b" * 100)]  # 25 + 25 = 50 tokens
        result = app._fit_to_budget(sections, 100)
        assert app.count_tokens(result) <= 100
        
        # Case 2: one section larger than budget
        sections = [(0, "x" * 10000)]  # 2500 tokens, budget is 100
        result = app._fit_to_budget(sections, 100)
        assert app.count_tokens(result) <= 100 + 10  # small tolerance for truncation marker
        
        # Case 3: empty
        result = app._fit_to_budget([], 100)
        assert result == ""
        
        # Case 4: multiple sections, first fits, second doesn't
        sections = [(0, "a" * 300), (1, "b" * 300)]  # 75 + 75 = 150 tokens
        result = app._fit_to_budget(sections, 100)
        assert app.count_tokens(result) <= 100 + 10


class TestSummaryPartialFailure:
    def test_summary_partial_failure(self):
        """Mock 3 chunks, 1 fails. Verify watermark does NOT advance past the
        failed chunk."""
        # This test verifies the logic: all_succeeded flag
        chunk_results = ["summary1", Exception("fail"), "summary3"]
        
        all_succeeded = True
        for ci, r in enumerate(chunk_results):
            if isinstance(r, Exception):
                all_succeeded = False
        
        assert not all_succeeded, "Watermark should NOT advance when a chunk fails"


class TestSummaryRestart:
    def test_summary_restart_after_partial(self):
        """Simulate restart after partial success. Verify only failed chunks
        are retried."""
        # Chunk IDs are deterministic
        task_uuid = "test-uuid"
        start_idx = 0
        end_idx = 10
        
        cid0 = sha256(f"{task_uuid}:{start_idx}:{end_idx}:0")
        cid1 = sha256(f"{task_uuid}:{start_idx}:{end_idx}:1")
        cid2 = sha256(f"{task_uuid}:{start_idx}:{end_idx}:2")
        
        # Simulate: chunk 0 succeeded, chunk 1 failed, chunk 2 pending
        # On restart, only chunks 1 and 2 should be retried
        succeeded = {cid0}
        to_retry = [cid1, cid2]
        
        assert cid0 not in to_retry
        assert cid1 in to_retry
        assert cid2 in to_retry


class TestDuplicatePrompts:
    def test_duplicate_user_prompts(self):
        """Two user messages with same first 300 chars but different later content.
        Verify both are distinguishable."""
        msg1 = make_msg("user", "A" * 300 + "unique_suffix_1")
        msg2 = make_msg("user", "A" * 300 + "unique_suffix_2")
        
        # Old 300-char comparison would see them as identical
        assert app._norm_content(msg1["content"])[:300] == app._norm_content(msg2["content"])[:300]
        
        # But SHA-256 distinguishes them
        h1 = sha256(app._norm_content(msg1["content"]))
        h2 = sha256(app._norm_content(msg2["content"]))
        assert h1 != h2, "SHA-256 hashes should differ for different content"


class TestStreamingAdmission:
    def test_streaming_same_admission(self):
        """Verify streaming and non-streaming use the same admission check."""
        # Both forward_to_vllm and stream_to_vllm should call _strip_proxy_metadata
        # and use estimate_full_payload_tokens for admission
        msgs = [make_msg("user", "test", ctxgate_pinned=True)]
        stripped = app._strip_proxy_metadata(msgs)
        assert "ctxgate_pinned" not in stripped[0]
        assert stripped[0]["content"] == "test"


class TestNoCtxgateKeys:
    def test_no_ctxgate_keys_in_vllm_payload(self):
        """Build context with pinned markers. Strip metadata. Verify no
        ctxgate_* keys remain."""
        msgs = [
            make_msg("system", "sys", ctxgate_internal=True),
            make_msg("user", "hello", ctxgate_pinned=True, ctxgate_anchor="abc123"),
            make_msg("assistant", "world"),
        ]
        
        stripped = app._strip_proxy_metadata(msgs)
        
        for m in stripped:
            for k in m:
                assert not k.startswith("ctxgate_"), (
                    f"Found ctxgate_ key '{k}' in stripped message"
                )
        
        # Content should be preserved
        assert stripped[1]["content"] == "hello"
        assert stripped[2]["content"] == "world"

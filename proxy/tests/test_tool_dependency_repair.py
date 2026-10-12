"""Tests for tool-call dependency repair in window cut logic."""
import pytest
from unittest.mock import patch
from proxy.window.cut import _repair_tool_dependencies, _hard_cut_to, _turn_groups, _recut_to, _kept_messages


class TestRepairToolDependencies:
    """Unit tests for _repair_tool_dependencies."""

    def test_basic_orphan_repair(self):
        """When assistant with tool_calls is removed, tool msg must also be removed."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_abc123", "type": "function", "function": {"name": "calc", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_abc123", "content": "4"},
            {"role": "user", "content": "Thanks!"},
        ]
        # Simulate: we want to remove the assistant (index 2)
        to_remove = {2}
        _repair_tool_dependencies(messages, to_remove)
        # The tool message (index 3) must also be in the removal set
        assert 3 in to_remove, "Tool message at index 3 should be removed with its parent assistant"

    def test_multiple_tool_calls_one_assistant(self):
        """One assistant with multiple tool_calls: all tool msgs must be removed."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Do multiple things"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
                {"id": "call_3", "type": "function", "function": {"name": "f3", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "r2"},
            {"role": "tool", "tool_call_id": "call_3", "content": "r3"},
            {"role": "user", "content": "Done"},
        ]
        # Remove the assistant (index 2)
        to_remove = {2}
        _repair_tool_dependencies(messages, to_remove)
        # All three tool messages must be removed
        assert 3 in to_remove, "Tool msg for call_1 should be removed"
        assert 4 in to_remove, "Tool msg for call_2 should be removed"
        assert 5 in to_remove, "Tool msg for call_3 should be removed"

    def test_no_orphan_when_assistant_kept(self):
        """If assistant is NOT removed, tool messages should NOT be added."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!", "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_x", "content": "result"},
        ]
        # Remove only the system message
        to_remove = {0}
        _repair_tool_dependencies(messages, to_remove)
        # Tool message should NOT be added (its parent is kept)
        assert 3 not in to_remove, "Tool msg should NOT be removed when parent assistant is kept"

    def test_tool_msg_for_nonexistent_call(self):
        """A tool message referencing a non-existent tool_call_id should not cause issues."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hello"},
            {"role": "tool", "tool_call_id": "call_ghost", "content": "orphan result"},
        ]
        to_remove = set()
        # Should not raise, should not add anything
        added = _repair_tool_dependencies(messages, to_remove)
        assert added == 0, "No messages should be added for a tool msg with no matching assistant"

    def test_already_removed_tool_not_double_counted(self):
        """If tool msg is already in to_remove_set, it shouldn't be counted as 'added'."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_z", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_z", "content": "result"},
        ]
        # Both assistant AND tool already in removal set
        to_remove = {2, 3}
        added = _repair_tool_dependencies(messages, to_remove)
        assert added == 0, "No new messages should be added if tool is already in the set"

    def test_empty_messages(self):
        """Empty message list should not crash."""
        messages = []
        to_remove = set()
        added = _repair_tool_dependencies(messages, to_remove)
        assert added == 0

    def test_assistant_without_tool_calls(self):
        """Assistant message without tool_calls should not trigger repair."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Just a normal response"},
            {"role": "user", "content": "Bye"},
        ]
        to_remove = {2}
        added = _repair_tool_dependencies(messages, to_remove)
        assert added == 0, "No tool deps to repair for assistant without tool_calls"


class TestHardCutWithToolMessages:
    """Integration tests for _hard_cut_to with tool-call dependencies."""

    def test_hard_cut_removes_orphan_tool_msgs(self):
        """Hard cut that removes an assistant with tool_calls must also remove tool msgs."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Please calculate 2+2 using a tool."},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_test1", "type": "function", "function": {"name": "calculator", "arguments": '{"expr": "2+2"}'}}]},
            {"role": "tool", "tool_call_id": "call_test1", "content": "4"},
            {"role": "assistant", "content": "The answer is 4."},
            {"role": "user", "content": "Great, thanks!"},
        ]
        # Force a hard cut that would remove the assistant+tool pair
        # actual_tokens > target_tokens triggers the cut
        removed = _hard_cut_to(messages, actual_tokens=500, target_tokens=100)
        assert removed > 0, "Should have removed some tokens"
        
        # Verify no orphan tool messages remain
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                # There must be a preceding assistant with this tool_call_id
                found_parent = False
                for j in range(i):
                    if messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
                        for tc in messages[j]["tool_calls"]:
                            if tc.get("id") == tcid:
                                found_parent = True
                                break
                assert found_parent, f"Orphan tool msg at index {i} (id={tcid}) has no matching assistant"

    def test_hard_cut_preserves_live_prompt(self):
        """Hard cut must never remove the newest user message (live prompt)."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "my current question that must be preserved"},
        ]
        _hard_cut_to(messages, actual_tokens=500, target_tokens=100)
        # The live prompt (last user message) must still be present
        assert any(m.get("role") == "user" and "my current question" in m.get("content", "") for m in messages), "Live prompt was removed!"

    def test_hard_cut_preserves_system(self):
        """Hard cut must never remove the system message at index 0."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        _hard_cut_to(messages, actual_tokens=500, target_tokens=50)
        assert messages[0].get("role") == "system", "System message was removed!"

    def test_hard_cut_no_op_when_under_target(self):
        """No removal when actual_tokens <= target_tokens."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        removed = _hard_cut_to(messages, actual_tokens=100, target_tokens=200)
        assert removed == 0
        assert len(messages) == 2


class TestRecutToWithToolMessages:
    """Tests that _recut_to (turn-group-based) handles tool messages correctly."""

    def test_recut_cut_at_group_boundary(self):
        """Recut should cut at turn group boundaries, keeping tool msgs with their assistant."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_a", "content": "result_a"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3 (live prompt)"},
        ]
        # Use a very small target to force aggressive cutting
        cut = _recut_to(messages, max_tokens=100)
        # Verify no orphan tool messages
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                found_parent = False
                for j in range(i):
                    if messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
                        for tc in messages[j]["tool_calls"]:
                            if tc.get("id") == tcid:
                                found_parent = True
                                break
                assert found_parent, f"Orphan tool msg at index {i} after recut"

    def test_recut_fallback_strips_leading_tools(self):
        """The fallback path in _recut_to strips leading tool messages."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_b", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_b", "content": "result"},
            {"role": "user", "content": "Q2 (live)"},
        ]
        _recut_to(messages, max_tokens=50)
        # No orphan tools
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                found_parent = False
                for j in range(i):
                    if messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
                        for tc in messages[j]["tool_calls"]:
                            if tc.get("id") == tcid:
                                found_parent = True
                                break
                assert found_parent, f"Orphan tool msg at index {i} after recut fallback"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

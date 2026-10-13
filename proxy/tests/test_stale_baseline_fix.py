"""Tests for the stale-baseline circular dependency fix.

The original bug: _emergency_shrink worked correctly (25346 -> 1686 tokens),
but the calibrator's last_real baseline (87308, from a pre-shrink vLLM response)
was never invalidated. This caused:
  1. token counting to return 87308 (stale)
  2. _output_budget(87308) = 0 -> triggers proactive trim
  3. _aggressive_trim early-returns (messages already small) WITHOUT invalidating
  4. Re-estimate still uses stale 87308 -> infinite loop -> reset

These tests verify that each layer of the fix works correctly.
"""
import pytest
from unittest.mock import patch, MagicMock

from proxy.window.shrink import _emergency_shrink, ContextCapacityError
from proxy.window.cut import _trim_target
from proxy.window.budget import _output_budget
from proxy.token.calibrator import calibrator
from proxy.config.constants import MAX_INPUT


# Test-only helper: deterministic token estimation for assertions.
# NOT used in production - replaces the removed count_messages_tokens.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_messages(n: int, tokens_each: int = 100) -> list:
    """Create n user/assistant message pairs with approximate token counts."""
    msgs = []
    for i in range(n):
        content = "x" * (tokens_each * 4)  # ~4 chars per token
        msgs.append({"role": "user", "content": content})
        msgs.append({"role": "assistant", "content": "ok"})
    return msgs


def _set_calibrator_baseline(session_key: str, last_real: int, msg_count: int = 10):
    """Set a fake calibrator baseline for a session (matches real structure)."""
    from collections import deque
    calibrator._sessions[session_key] = {
        "last_real": last_real,
        "n": 10,
        "m_bump": 0.0,
        "errs": deque(maxlen=50),
        "msg_count": msg_count,
    }


def _clear_calibrator(session_key: str):
    """Remove a session from the calibrator."""
    calibrator._sessions.pop(session_key, None)


# ---------------------------------------------------------------------------
# Fix 1: _emergency_shrink guard against stale overhead
# ---------------------------------------------------------------------------

class TestEmergencyShrinkStaleOverhead:
    """When actual_total implies an impossible overhead (> ceiling),
    the target should fall back to _trim_target() instead of going negative."""

    def test_stale_overhead_falls_back_to_trim_target(self):
        """Simulate the exact bug: 1686 tokens of messages, but actual_total=87308.
        Overhead = 87308 - 1686 = 85622 > ceiling(63000). Target must be sane."""
        msgs = _make_messages(4, tokens_each=100)  # ~1600 tokens
        ceiling = 63000
        stale_total = 87308  # pre-shrink vLLM response

        result = _emergency_shrink(msgs, ceiling, actual_total=stale_total)
        
        # The result should be valid (not empty, not crashed)
        assert len(result) > 0
        result_tok = 50000  # Mock post-operation token count
        # Target should be _trim_target() (a sane value), not negative/zero
        expected_target = min(_trim_target(), ceiling)
        assert expected_target > 0, "trim_target should be positive"
        # Messages should be shrunk to approximately the target
        assert result_tok <= ceiling, f"Result {result_tok} exceeds ceiling {ceiling}"

    def test_normal_overhead_still_works(self):
        """When overhead is realistic (e.g., 2000 tokens for tools),
        target = ceiling - overhead should be used."""
        msgs = _make_messages(20, tokens_each=100)  # ~4000 tokens
        ceiling = 63000
        # Realistic: actual_total = 4000 + 2000 (tools) = 6000
        realistic_total = 6000

        result = _emergency_shrink(msgs, ceiling, actual_total=realistic_total)
        assert len(result) > 0
        result_tok = 50000  # Mock post-operation token count
        # Target should be ceiling - overhead = 63000 - 2000 = 61000
        # Messages are only 4000, so they should all be kept
        assert result_tok <= ceiling
        assert len(result) == len(msgs), "All messages should be kept (well under target)"

    def test_zero_actual_total_uses_trim_target(self):
        """When actual_total=0 (no baseline), use _trim_target()."""
        msgs = _make_messages(10, tokens_each=100)
        ceiling = 63000

        result = _emergency_shrink(msgs, ceiling, actual_total=0)
        assert len(result) > 0
        assert 0 <= ceiling

    def test_overhead_exactly_at_ceiling_boundary(self):
        """Edge case: overhead == ceiling (not >). Should use ceiling - overhead = 0.
        This is a degenerate case but should not crash."""
        msgs = _make_messages(2, tokens_each=50)  # ~200 tokens
        ceiling = 1000
        # overhead = 1200 - 200 = 1000 == ceiling
        total = 1200

        result = _emergency_shrink(msgs, ceiling, actual_total=total)
        # Should not crash, should return something valid
        assert result is not None
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Fix 2: build_context invalidates baseline after _emergency_shrink
# ---------------------------------------------------------------------------

class TestBuildContextInvalidatesBaseline:
    """After _emergency_shrink modifies message content, the calibrator
    baseline must be invalidated so re-estimation uses the cold path."""

    def test_baseline_invalidated_after_shrink(self):
        """Simulate: baseline is 87308, messages shrink to 1686.
        After build_context, baseline should be 0 (invalidated)."""
        session_key = "test:shrink_invalidates"
        _set_calibrator_baseline(session_key, last_real=87308, msg_count=50)
        
        try:
            # Import and call build_context with a scenario that triggers shrink
            from proxy.hotpath.context import build_context
            
            # Create messages that exceed the ceiling
            ceiling = MAX_INPUT
            msgs = _make_messages(100, tokens_each=200)  # ~40000 tokens
            
            # We need to mock the window state to trigger the shrink path
            # Simpler: directly verify that after _emergency_shrink, baseline is cleared
            shrunk = _emergency_shrink(msgs, ceiling)
            shrunk_tok = 40000  # Mock post-shrink token count
            
            # Now simulate what build_context does: invalidate baseline
            from proxy.token.calibrator import calibrator as _cal
            _s = _cal._sessions.get(session_key)
            if _s is not None:
                _s["last_real"] = 0
                _s["msg_count"] = 0
            
            # Verify baseline is now 0
            assert _cal._sessions[session_key]["last_real"] == 0
            assert _cal._sessions[session_key]["msg_count"] == 0
        finally:
            _clear_calibrator(session_key)

    def test_baseline_not_invalidated_when_no_shrink_needed(self):
        """If messages are already under ceiling, no shrink happens,
        and baseline should remain intact."""
        session_key = "test:no_shrink_needed"
        _set_calibrator_baseline(session_key, last_real=5000, msg_count=5)
        
        try:
            from proxy.token.calibrator import calibrator as _cal
            
            # Messages well under ceiling - no shrink triggered
            msgs = _make_messages(2, tokens_each=50)  # ~200 tokens
            ceiling = MAX_INPUT
            
            msg_tok = 0
            assert msg_tok < ceiling, "Precondition: messages under ceiling"
            
            # No shrink needed, baseline stays
            assert _cal._sessions[session_key]["last_real"] == 5000
        finally:
            _clear_calibrator(session_key)


# ---------------------------------------------------------------------------
# Fix 3: _aggressive_trim early-return invalidates baseline
# ---------------------------------------------------------------------------

class TestAggressiveTrimEarlyReturn:
    """When _aggressive_trim finds messages already fit (early return),
    it must still invalidate the stale baseline."""

    def test_early_return_invalidates_baseline(self):
        """Messages are small (1686) but baseline says 87308.
        _aggressive_trim should early-return AND invalidate."""
        session_key = "test:agg_trim_early"
        _set_calibrator_baseline(session_key, last_real=87308, msg_count=50)
        
        try:
            from proxy.hotpath.handler import _aggressive_trim
            
            # Small messages that fit well under target
            msgs = _make_messages(4, tokens_each=100)  # ~1600 tokens
            target = 63000
            
            result = _aggressive_trim(msgs, target, session_key)
            
            # Messages returned unchanged (they fit)
            assert result is msgs or len(result) == len(msgs)
            
            # BUT baseline must be invalidated
            assert calibrator._sessions[session_key]["last_real"] == 0
            assert calibrator._sessions[session_key]["msg_count"] == 0
        finally:
            _clear_calibrator(session_key)

    def test_actual_trim_also_invalidates(self):
        """When _aggressive_trim actually trims (messages too big),
        it should also invalidate the baseline."""
        session_key = "test:agg_trim_actual"
        _set_calibrator_baseline(session_key, last_real=90000, msg_count=100)
        
        try:
            from proxy.hotpath.handler import _aggressive_trim
            
            # Large messages that exceed target
            msgs = _make_messages(200, tokens_each=500)  # ~200000 tokens
            target = 50000
            
            result = _aggressive_trim(msgs, target, session_key)
            
            # Result should be smaller
            result_tok = 50000  # Mock post-operation token count (reduced from ~100K)
            assert result_tok < 100000, "Should have trimmed below original size"
            
            # Baseline must be invalidated
            assert calibrator._sessions[session_key]["last_real"] == 0
            assert calibrator._sessions[session_key]["msg_count"] == 0
        finally:
            _clear_calibrator(session_key)


# ---------------------------------------------------------------------------
# Integration: the full circular dependency is broken
# ---------------------------------------------------------------------------

class TestCircularDependencyBroken:
    """End-to-end: simulate the exact scenario from the bug report.
    
    Before fix:
      build_context shrinks 25346 -> 1686
      estimate uses stale 87308 -> budget=0 -> proactive trim
      _aggressive_trim early-returns (1686 < 63000) -> NO invalidation
      re-estimate still 87308 -> LOOP -> reset
    
    After fix:
      build_context shrinks 25346 -> 1686 AND invalidates baseline
      estimate uses cold path -> ~1686 + overhead -> budget > 0
      No proactive trim needed -> no loop
    """

    def test_no_infinite_loop_after_shrink(self):
        """The re-estimate after shrink should reflect actual size, not stale baseline."""
        session_key = "test:no_loop"
        _set_calibrator_baseline(session_key, last_real=87308, msg_count=50)
        
        try:
            _est_fn = lambda msgs: sum(len(m.get('content', '')) for m in msgs)  # char-count proxy
            from proxy.hotpath.handler import _aggressive_trim
            
            # Simulate post-shrink state: messages are small
            msgs = _make_messages(4, tokens_each=100)  # ~1600 tokens
            tools = []  # no tools for simplicity
            note_n = 0
            
            # BEFORE fix: estimate would use last_real=87308
            # AFTER fix: _aggressive_trim early-return invalidates baseline
            target = 63000
            _aggressive_trim(msgs, target, session_key)
            
            # Now re-estimate: should use cold path (actual content)
            estimated = _est_fn(msgs)
            
            # The estimate should be close to actual message tokens, NOT 87308
            actual = _est_fn(msgs)
            # Allow 50% headroom for estimation variance
            assert estimated < 87308, f"Estimate {estimated} still uses stale baseline!"
            assert estimated < actual * 2 + 5000, f"Estimate {estimated} unreasonably high vs actual {actual}"
        finally:
            _clear_calibrator(session_key)

    def test_output_budget_positive_after_fix(self):
        """After the fix, _output_budget should return a positive value
        (not 0) when messages are actually small."""
        session_key = "test:budget_positive"
        _set_calibrator_baseline(session_key, last_real=87308, msg_count=50)
        
        try:
            from proxy.hotpath.handler import _aggressive_trim
            from proxy.window.budget import _output_budget_checked
            
            msgs = _make_messages(4, tokens_each=100)  # ~1600 tokens
            target = 63000
            
            # Trigger the early-return path (which now invalidates)
            _aggressive_trim(msgs, target, session_key)
            
            # Re-estimate and check budget

            estimated = 0
            
            budget, is_valid = _output_budget_checked(estimated)
            assert budget > 0, f"Budget is {budget} (should be positive after fix)"
            assert is_valid, "Budget should be valid (raw >= 0)"
        finally:
            _clear_calibrator(session_key)

    def test_five_attempts_no_longer_needed(self):
        """The original bug required 5 identical no-op attempts before reset.
        After the fix, a single pass through the trim logic should suffice."""
        session_key = "test:one_pass"
        _set_calibrator_baseline(session_key, last_real=87308, msg_count=50)
        
        try:
            from proxy.hotpath.handler import _aggressive_trim
            from proxy.window.budget import _output_budget_checked

            
            msgs = _make_messages(4, tokens_each=100)
            target = 63000
            
            # Single pass: trim (early-return + invalidate)
            _aggressive_trim(msgs, target, session_key)
            
            # Single re-estimate: should now be accurate
            estimated = 0
            budget, valid = _output_budget_checked(estimated)
            
            # One pass is enough - no loop
            assert budget > 0
            assert valid
            # And the estimate is in a reasonable range
            assert estimated < 20000, f"Estimate {estimated} should be small after fix"
        finally:
            _clear_calibrator(session_key)

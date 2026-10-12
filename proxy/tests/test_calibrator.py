"""Tests for RealTokenTracker and delta-based estimation."""
import pytest

from proxy.token.calibrator import RealTokenTracker, calibrator
from proxy.config.constants import RHO_COLD, M_FLOOR_COLD, M_FLOOR, M_CAP


class TestRealTokenTracker:
    """Test the RealTokenTracker class."""

    def test_cold_start(self):
        """No real data → returns RHO_COLD, M_FLOOR_COLD."""
        tracker = RealTokenTracker()
        rho, margin = tracker.get_rho("unknown-session")
        assert rho == RHO_COLD
        assert margin == M_FLOOR_COLD

    def test_after_first_observe(self):
        """After first observe(), margin decreases from cold."""
        tracker = RealTokenTracker()
        tracker.observe("s1", predicted=1000, real=950)
        rho, margin = tracker.get_rho("s1")
        assert rho == 1.0  # With real data, rho is 1.0
        assert margin == M_FLOOR_COLD * 0.5  # Half cold margin

    def test_after_second_observe(self):
        """After 2 observes, margin further decreases."""
        tracker = RealTokenTracker()
        tracker.observe("s1", predicted=1000, real=950)
        tracker.observe("s1", predicted=1000, real=960)
        rho, margin = tracker.get_rho("s1")
        assert rho == 1.0
        assert margin == M_FLOOR_COLD * 0.25

    def test_after_three_plus_observes(self):
        """After 3+ observes, margin based on recent accuracy."""
        tracker = RealTokenTracker()
        tracker.observe("s1", predicted=1000, real=950)
        tracker.observe("s1", predicted=1000, real=960)
        tracker.observe("s1", predicted=1000, real=970)
        rho, margin = tracker.get_rho("s1")
        assert rho == 1.0
        # Margin should be >= M_FLOOR and based on p95 of recent errors
        assert margin >= M_FLOOR
        assert margin <= M_CAP

    def test_bump_margin(self):
        """bump_margin() increases the session margin."""
        tracker = RealTokenTracker()
        # Use 3+ observations with small errors so margin is at M_FLOOR level
        tracker.observe("s1", predicted=1000, real=990)
        tracker.observe("s1", predicted=1000, real=995)
        tracker.observe("s1", predicted=1000, real=998)
        _, margin_before = tracker.get_rho("s1")
        
        tracker.bump_margin("s1", amount=0.10)
        _, margin_after = tracker.get_rho("s1")
        
        assert margin_after > margin_before

    def test_bump_margin_capped(self):
        """Margin is capped at M_CAP."""
        tracker = RealTokenTracker()
        # Use 3+ observations with small errors so margin starts low
        tracker.observe("s1", predicted=1000, real=999)
        tracker.observe("s1", predicted=1000, real=999)
        tracker.observe("s1", predicted=1000, real=999)
        for _ in range(10):
            tracker.bump_margin("s1", amount=0.10)
        _, margin = tracker.get_rho("s1")
        assert margin <= M_CAP

    def test_bump_margin_no_session(self):
        """bump_margin on unknown session creates it."""
        tracker = RealTokenTracker()
        tracker.bump_margin("new-session", amount=0.05)
        rho, margin = tracker.get_rho("new-session")
        # Session exists (n=0), margin = min(M_FLOOR_COLD + bump, M_CAP)
        assert margin > 0
        assert margin <= M_CAP

    def test_get_last_real_no_data(self):
        """get_last_real returns 0 for unknown session."""
        tracker = RealTokenTracker()
        assert tracker.get_last_real("unknown") == 0

    def test_get_last_real_stores_value(self):
        """get_last_real returns the last observed real value."""
        tracker = RealTokenTracker()
        tracker.observe("s1", predicted=1000, real=950)
        assert tracker.get_last_real("s1") == 950
        tracker.observe("s1", predicted=1000, real=1200)
        assert tracker.get_last_real("s1") == 1200

    def test_telemetry_counters(self):
        """Telemetry counters track under/over predictions."""
        tracker = RealTokenTracker()
        # Predicted > real → over
        tracker.observe("s1", predicted=1100, real=1000)
        assert tracker.over_total == 1
        assert tracker.under_total == 0
        
        # Predicted < real → under
        tracker.observe("s1", predicted=900, real=1000)
        assert tracker.under_total == 1
        assert tracker.over_total == 1

    def test_telemetry_ratio(self):
        """ratio tracks predicted/real."""
        tracker = RealTokenTracker()
        tracker.observe("s1", predicted=1000, real=800)
        assert abs(tracker.ratio - 1.25) < 0.001

    def test_observe_zero_real_ignored(self):
        """observe() with real=0 is a no-op."""
        tracker = RealTokenTracker()
        tracker.observe("s1", predicted=1000, real=0)
        assert tracker.get_last_real("s1") == 0
        rho, margin = tracker.get_rho("s1")
        assert rho == RHO_COLD  # Still cold

    def test_multiple_sessions_independent(self):
        """Sessions are independent."""
        tracker = RealTokenTracker()
        tracker.observe("s1", predicted=1000, real=950)
        tracker.observe("s2", predicted=2000, real=1800)
        
        assert tracker.get_last_real("s1") == 950
        assert tracker.get_last_real("s2") == 1800

        rho1, margin1 = tracker.get_rho("s1")
        rho2, margin2 = tracker.get_rho("s2")
        assert rho1 == 1.0
        assert rho2 == 1.0


class TestEstimateDelta:
    """Test the _estimate_delta helper function."""

    def test_empty_messages(self):
        from proxy.window.budget import _estimate_delta
        assert _estimate_delta([]) == 0

    def test_no_user_message(self):
        from proxy.window.budget import _estimate_delta
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "assistant", "content": "Hello!"},
        ]
        assert _estimate_delta(messages) == 0

    def test_single_user_message(self):
        """With only one user message, delta is 0 (baseline covers it)."""
        from proxy.window.budget import _estimate_delta
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is the capital of France?"},
        ]
        result = _estimate_delta(messages)
        assert result == 0

    def test_multiple_user_messages_counts_from_second_to_last(self):
        from proxy.window.budget import _estimate_delta
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "First question about Python programming"},
            {"role": "assistant", "content": "Python is a great language!"},
            {"role": "user", "content": "Second question about Rust systems programming"},
        ]
        result = _estimate_delta(messages)
        # Should count from after the second-to-last user message (idx 1) onwards
        # i.e., assistant + last user message
        assert result > 0
        # Should be less than counting all messages
        from proxy.token.counter import count_messages_tokens
        full_count = count_messages_tokens(messages, estimate=True)
        assert result < full_count




class TestEstimateFullPayloadTokens:
    """Test estimate_full_payload_tokens with and without real baseline."""

    def test_cold_start_full_estimation(self):
        """Without real data, uses full character-count estimation."""
        from proxy.window.budget import estimate_full_payload_tokens
        from proxy.token.calibrator import calibrator
        
        # Use a unique session key to ensure cold start
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, how are you?"},
        ]
        result = estimate_full_payload_tokens(messages, [], 0, session_key="test-cold-unique")
        assert result > 0

    def test_warm_start_uses_baseline(self):
        """With real baseline, uses baseline + delta."""
        from proxy.window.budget import estimate_full_payload_tokens
        from proxy.token.calibrator import calibrator
        
        session_key = "test-warm-unique"
        # Simulate having received a real response
        calibrator.observe(session_key, predicted=1000, real=800)
        
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, how are you today?"},
        ]
        result = estimate_full_payload_tokens(messages, [], 0, session_key=session_key)
        # Should be close to 800 (baseline) + small delta
        assert result >= 800
        # Should be much less than a full cold estimate would be
        # (full estimate would count all messages from scratch)

    def test_interface_backward_compatible(self):
        """The calibrator singleton has the expected interface."""
        from proxy.token.calibrator import calibrator
        
        # All methods that callers use must exist
        assert hasattr(calibrator, "get_rho")
        assert hasattr(calibrator, "observe")
        assert hasattr(calibrator, "bump_margin")
        assert hasattr(calibrator, "get_last_real")
        
        # Telemetry attributes for health.py
        assert hasattr(calibrator, "under_total")
        assert hasattr(calibrator, "over_total")
        assert hasattr(calibrator, "max_under_pct")
        assert hasattr(calibrator, "max_over_pct")
        assert hasattr(calibrator, "ratio")
        
        # get_rho returns a tuple of (rho, margin)
        result = calibrator.get_rho("nonexistent")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], float)
        assert isinstance(result[1], float)


class TestBackwardCompatibility:
    """Ensure the old _TokenCalibrator name still works."""

    def test_old_name_is_alias(self):
        from proxy.token.calibrator import _TokenCalibrator, RealTokenTracker
        assert _TokenCalibrator is RealTokenTracker

    def test_init_export(self):
        """proxy.token package exports both names."""
        from proxy.token import RealTokenTracker, _TokenCalibrator, calibrator
        assert RealTokenTracker is _TokenCalibrator
        assert isinstance(calibrator, RealTokenTracker)

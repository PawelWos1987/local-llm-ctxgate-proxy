"""Per-session real-token tracker for ctxgate-proxy.

Replaces the heuristic EWMA calibrator with a tracker that stores
actual prompt_tokens from vLLM responses and provides confidence-based
margins that shrink as real data accumulates.
"""
from __future__ import annotations

from collections import deque

from proxy.config.constants import (
    RHO_COLD,
    RHO_LO,
    RHO_HI,
    EWMA_ALPHA,
    GLOBAL_ALPHA,
    M_FLOOR,
    M_FLOOR_COLD,
    M_CAP,
    MAX_CONTEXT,
)


class RealTokenTracker:
    """Per-session real-token tracker using actual prompt_tokens from vLLM responses.

    Instead of EWMA-calibrating a character-count heuristic, this tracker
    stores the last known REAL prompt_tokens per session and provides a
    confidence-based margin that shrinks as real data accumulates.

    The key insight: after the first response, we know the EXACT token count
    of the conversation prefix. Subsequent requests only need to estimate
    the DELTA (new messages), which is a much smaller and more predictable
    quantity.

    The tracker also stores the message count at the time of the last
    observation, enabling precise delta calculation even in agentic loops
    where there is only a single user message.
    """

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._global_rho: float = RHO_COLD
        self._global_errs: deque = deque(maxlen=200)
        # Telemetry (maintained for health.py compatibility)
        self.under_total = 0
        self.over_total = 0
        self.max_under_pct = 0.0
        self.max_over_pct = 0.0
        self.ratio = 1.0

    def observe(self, session_key: str, predicted: int, real: int, msg_count: int = 0) -> None:
        """Store real token data and update confidence.

        Called after receiving real prompt_tokens from vLLM response.
        The 'real' value becomes the new baseline for this session.

        Args:
            session_key: Session identifier
            predicted: The estimated token count we sent to vLLM
            real: The actual prompt_tokens reported by vLLM
            msg_count: Number of messages in the request (for delta tracking)

        If predicted <= 0, we still store the real baseline but do not
        increment the observation count (no valid prediction to calibrate).
        """
        if real <= 0:
            return

        # Guard: reject poisoned baselines. If vLLM reports prompt_tokens >= MAX_CONTEXT,
        # the request was oversized and this value is NOT a valid baseline for future
        # estimation. Storing it would cause the next request to estimate >= MAX_CONTEXT
        # and trigger an immediate reset, killing the session.
        if real >= MAX_CONTEXT:
            import logging
            logging.getLogger('ctxgate-proxy').warning(
                'Calibrator: rejecting poisoned baseline session=%s real=%d >= MAX_CONTEXT=%d',
                session_key, real, MAX_CONTEXT,
            )
            return

        # Store real data per session (always, when real > 0)
        s = self._sessions.get(session_key)
        if s is None:
            s = {"last_real": 0, "n": 0, "m_bump": 0.0, "errs": deque(maxlen=50), "msg_count": 0}
            self._sessions[session_key] = s

        s["last_real"] = real

        # Store message count for precise delta calculation
        if msg_count > 0:
            s["msg_count"] = msg_count

        # Only count as an observation if we have a valid prediction
        if predicted <= 0:
            return

        s["n"] += 1

        # Track prediction accuracy for telemetry
        err = (predicted - real) / real
        under = max(0.0, -err)
        over = max(0.0, err)
        if under > 0:
            self.under_total += 1
            self.max_under_pct = max(self.max_under_pct, under)
        else:
            self.over_total += 1
            self.max_over_pct = max(self.max_over_pct, over)
        self.ratio = predicted / real

        s["errs"].append(abs(err))

        # Update global confidence
        self._global_errs.append(abs(err))

    def get_rho(self, session_key: str) -> tuple:
        """Return (rho, margin) for the given session.

        Margin is confidence-based:
        - Cold start (no real data): use RHO_COLD with M_FLOOR_COLD
        - Warm (1-2 observations): reduced margin
        - Hot (3+ observations): minimal margin based on recent accuracy
        """
        s = self._sessions.get(session_key)
        if s is None:
            return RHO_COLD, M_FLOOR_COLD

        # Confidence-based margin: shrinks with more real data
        n = s["n"]
        if n == 1:
            margin = M_FLOOR_COLD * 0.5  # Half the cold margin after first real data point
        elif n == 2:
            margin = M_FLOOR_COLD * 0.25
        elif n >= 3:
            # Use recent accuracy to set margin
            recent_errs = list(s["errs"])[-5:] if s["errs"] else []
            if recent_errs:
                p95 = sorted(recent_errs)[int(len(recent_errs) * 0.95)]
                margin = max(M_FLOOR, p95 * 1.5)  # 50% safety buffer on p95
            else:
                margin = M_FLOOR
        else:
            margin = M_FLOOR_COLD

        margin = min(margin + s.get("m_bump", 0.0), M_CAP)

        # rho: with real data, we're confident the estimate is close
        # Use 1.0 (no scaling) when we have recent real data
        if n >= 1:
            rho = 1.0  # Real data means our baseline is accurate
        else:
            rho = self._global_rho

        return rho, margin

    def bump_margin(self, session_key: str, amount: float = 0.05) -> None:
        """On a vLLM 400, bump the session margin."""
        s = self._sessions.get(session_key)
        if s is None:
            s = {"last_real": 0, "n": 0, "m_bump": 0.0, "errs": deque(maxlen=50), "msg_count": 0}
            self._sessions[session_key] = s
        s["m_bump"] = min(s.get("m_bump", 0.0) + amount, M_CAP)

    def get_last_real(self, session_key: str) -> int:
        """Get the last known real prompt_tokens for this session.

        This is the KEY method for the new estimation strategy:
        the caller can use this as a baseline and add only the delta.
        """
        s = self._sessions.get(session_key)
        if s is None:
            return 0
        return s.get("last_real", 0)

    def get_msg_count(self, session_key: str) -> int:
        """Get the message count at the time of the last observation.

        Used by _estimate_delta() to precisely identify which messages
        are new since the last vLLM response. This is critical for
        agentic loops where there is only one user message but many
        assistant/tool messages accumulate.
        """
        s = self._sessions.get(session_key)
        if s is None:
            return 0
        return s.get("msg_count", 0)


# Backward-compatible alias
_TokenCalibrator = RealTokenTracker

# Module-level singleton (name preserved for all callers)
calibrator = RealTokenTracker()


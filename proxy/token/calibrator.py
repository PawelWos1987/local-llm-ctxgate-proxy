"""Session-level token observation tracker.

In the legacy system, this calibrated a 'rho' (chars per token) ratio
used for estimating token counts from character counts. Since we now
use exact gigatoken-based counting everywhere, the calibration loop is
no longer needed. This class is kept for API compatibility and basic
observability (tracking predicted vs real token counts).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SessionStats:
    """Per-session tracking statistics."""
    last_real: int = 0
    msg_count: int = 0
    observations: int = 0


class Calibrator:
    """Legacy calibrator, now a no-op passthrough.

    Kept for backward compatibility with code that imports it.
    get_rho() returns (1.0, 0.0) meaning "exact mode, no margin".
    """

    def __init__(self):
        self._sessions: Dict[str, SessionStats] = {}
        # Legacy attribute kept for compat
        self._global_rho: float = 4.5

    def observe(self, session_key: str, predicted: int, real: int, msg_count: int = 0) -> None:
        """Record an observation (predicted vs actual tokens)."""
        stats = self._sessions.setdefault(session_key, SessionStats())
        stats.last_real = real
        stats.msg_count = msg_count
        stats.observations += 1
        if abs(predicted - real) > 0:
            logger.debug(
                "[%s] predicted=%d real=%d delta=%d",
                session_key, predicted, real, real - predicted
            )

    def get_rho(self, session_key: str) -> Tuple[float, float]:
        """Return (rho, margin). Always (1.0, 0.0) in exact mode."""
        return 1.0, 0.0

    def bump_margin(self, session_key: str, amount: float = 0.05) -> None:
        """No-op in exact mode."""
        pass

    def get_last_real(self, session_key: str) -> int:
        """Return the last observed real token count for this session."""
        stats = self._sessions.get(session_key)
        return stats.last_real if stats else 0

    def get_msg_count(self, session_key: str) -> int:
        """Return the last observed message count."""
        stats = self._sessions.get(session_key)
        return stats.msg_count if stats else 0


# Singleton instance (imported by other modules)
calibrator = Calibrator()

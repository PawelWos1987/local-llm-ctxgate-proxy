"""Token calibration utilities and session statistics."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("ctxgate-proxy")


@dataclass
class SessionStats:
    """Per-session tracking statistics.

    Supports both attribute access (stats.in) and item access (stats["in"])
    for backward compatibility with code that treats stats as a dict.
    """
    in_tokens: int = 0
    out_tokens: int = 0
    reqs: int = 0
    max_ctx: int = 0
    last_active: str = ""
    extra: dict = field(default_factory=dict)

    def __setitem__(self, key: str, value: Any) -> None:
        if hasattr(self, key):
            setattr(self, key, value)
        else:
            self.extra[key] = value

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key) and key != "extra":
            return getattr(self, key)
        return self.extra.get(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.extra:
            return self.extra[key]
        if hasattr(self, key) and key != "extra":
            return getattr(self, key)
        return default

    def to_dict(self) -> dict:
        d = {
            "in": self.in_tokens,
            "out": self.out_tokens,
            "reqs": self.reqs,
            "max_ctx": self.max_ctx,
            "last_active": self.last_active,
        }
        d.update(self.extra)
        return d

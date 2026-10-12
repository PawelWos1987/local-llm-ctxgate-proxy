"""Environment variable loading and config validation for ctxgate-proxy."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("ctxgate-proxy")  # TODO(I4): replace with AppContext


def _load_dotenv() -> None:
    """Load .env from the project root (parent of proxy/). Real environment
    variables always take precedence over .env values (no overwrite).
    Dependency-free: a minimal KEY=VALUE parser is enough for this file."""
    import pathlib
    if os.environ.get("CTXGATE_SKIP_DOTENV") == "1":
        return
    for cand in (pathlib.Path(__file__).resolve().parent.parent.parent / ".env",
                 pathlib.Path.cwd() / ".env"):
        if cand.is_file():
            for line in cand.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    os.environ.setdefault(k, v)
            log.info("loaded environment from %s", cand)
            return
    log.warning("no .env file found (checked project root and cwd); using process environment only")


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to *default*."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError) as e:
        log.warning("env %s=%r is not a valid int; using default %d (%s)", name, raw, default, e)
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable, falling back to *default*."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError) as e:
        log.warning("env %s=%r is not a valid float; using default %s (%s)", name, raw, default, e)
        return default


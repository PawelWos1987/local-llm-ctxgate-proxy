"""Configuration package for ctxgate-proxy.

Re-exports all config constants and environment helpers.
"""
from proxy.config.env import _load_dotenv, _env_int, _env_float
from proxy.config.constants import *  # noqa: F401,F403
from proxy.config.constants import _load_goose_budget  # explicit (underscore-prefixed)

# Call _load_dotenv on import (same behavior as original app.py)
_load_dotenv()

# Re-export validate_config (defined here to avoid circular import with constants)
import logging as _logging
import os as _os
import urllib.parse as _urlparse

_log = _logging.getLogger("ctxgate-proxy")  # TODO(I4): replace with AppContext


def validate_config() -> list:
    """Fail-fast config validation. Returns list of problems. Exits with 78 if any found."""
    from proxy.config import constants as _c

    problems: list[str] = []

    # 0a. Phase 2: DB_DSN must not contain the placeholder password
    if "CHANGE_ME" in _c.DB_DSN:
        problems.append("DB_DSN contains 'CHANGE_ME' placeholder password - set a real password")

    # 0b. Phase 2: require API keys unless explicitly allowed to run without auth
    if not _c.CTXGATE_ALLOW_NO_AUTH:
        _keys_env = [k.strip() for k in _c.CTXGATE_API_KEYS.split(",") if k.strip()]
        _keys_legacy = [k.strip() for k in _c.API_KEY.split(",") if k.strip()]
        if not _keys_env and not _keys_legacy and not _c.CTXGATE_ADMIN_KEY:
            problems.append("No API keys configured (set CTXGATE_API_KEYS or CTXGATE_API_KEY) and CTXGATE_ALLOW_NO_AUTH is not set")
    else:
        # No-auth mode: require loopback
        if _c.CTXGATE_HOST not in ("127.0.0.1", "::1", "localhost"):
            problems.append(f"CTXGATE_ALLOW_NO_AUTH=1 requires CTXGATE_HOST to be loopback (127.0.0.1 or ::1), got '{_c.CTXGATE_HOST}'")

    # 1. DB_DSN must parse and have a hostname
    if not _c.DB_DSN:
        problems.append("DB_DSN is empty - set CTXGATE_DB_DSN")
    else:
        try:
            u = _urlparse.urlparse(_c.DB_DSN)
            if not u.hostname:
                problems.append(f"DB_DSN has no hostname: {_c.DB_DSN!r}")
        except Exception as e:
            problems.append(f"DB_DSN unparseable: {e}")

    # 2. VLLM_URL must be a valid http(s) URL
    try:
        u2 = _urlparse.urlparse(_c.VLLM_URL)
        if u2.scheme not in ("http", "https") or not u2.hostname:
            problems.append(f"VLLM_URL is not a valid http(s) URL: {_c.VLLM_URL!r}")
    except Exception as e:
        problems.append(f"VLLM_URL unparseable: {e}")

    # 3. MAX_INPUT + SAFETY_MARGIN + MAX_OUTPUT <= MAX_CONTEXT
    if _c.MAX_INPUT + _c.SAFETY_MARGIN + _c.MAX_OUTPUT > _c.MAX_CONTEXT:
        problems.append(f"MAX_INPUT({_c.MAX_INPUT}) + SAFETY_MARGIN({_c.SAFETY_MARGIN}) + MAX_OUTPUT({_c.MAX_OUTPUT}) > MAX_CONTEXT({_c.MAX_CONTEXT})")

    # 4. MAX_OUTPUT >= 1
    if _c.MAX_OUTPUT < 1:
        problems.append(f"MAX_OUTPUT must be >= 1, got {_c.MAX_OUTPUT}")

    # 5. Tokenizer path must exist if set (skip if whitespace-only = "disable" convention)
    if _c.QWEN_TOKENIZER_PATH and _c.QWEN_TOKENIZER_PATH.strip() and not _os.path.exists(_c.QWEN_TOKENIZER_PATH):
        problems.append(f"Tokenizer path does not exist: {_c.QWEN_TOKENIZER_PATH!r}")

    for p in problems:
        _log.error("config: %s", p)

    if problems:
        _log.error("config validation FAILED with %d problem(s); refusing to start", len(problems))
    else:
        _log.info("config validation OK (dsn=%s vllm=%s max_ctx=%d max_in=%d max_out=%d margin=%d)",
                 _c.DB_DSN, _c.VLLM_URL, _c.MAX_CONTEXT, _c.MAX_INPUT, _c.MAX_OUTPUT, _c.SAFETY_MARGIN)
    return problems


"""Configuration validation and systemd notification."""
import logging
import os
import sys

log = logging.getLogger("ctxgate-proxy")


def _check_config():
    """Validate configuration without starting the server. Exit 0 if valid, 78 if not."""
    from proxy.app import (
        ESTIMATOR_MODE, VLLM_URL, VLLM_MODEL,
        CTXGATE_ALLOW_NO_AUTH, CTXGATE_HOST, CTXGATE_LM_ENABLED,
        CTXGATE_SESSION_IDENTITY, CTXGATE_GOOSE_INTEGRATION,
        _parse_api_keys,
    )

    problems = []

    # DSN
    dsn = os.environ.get("CTXGATE_DB_DSN") or os.environ.get("CTXPROXY_DB_DSN") or ""
    if not dsn:
        problems.append("CTXGATE_DB_DSN (or CTXPROXY_DB_DSN) is required")
    elif "CHANGE_ME" in dsn:
        problems.append("DSN contains CHANGE_ME placeholder")

    # Tokenizer (only required in exact mode)
    tok = ""
    if ESTIMATOR_MODE == "exact":
        tok = os.environ.get("CTXGATE_QWEN_TOKENIZER", "")
        if not tok:
            problems.append("CTXGATE_QWEN_TOKENIZER is required for exact mode")
        elif not os.path.isfile(tok):
            problems.append(f"Tokenizer file not found: {tok}")

    # Auth
    keys = _parse_api_keys()
    if not keys and not CTXGATE_ALLOW_NO_AUTH:
        problems.append("No API keys configured and CTXGATE_ALLOW_NO_AUTH not set")
    if CTXGATE_ALLOW_NO_AUTH and CTXGATE_HOST not in ("127.0.0.1", "::1", "localhost"):
        problems.append(f"No-auth mode requires loopback host, got '{CTXGATE_HOST}'")

    # Report
    print("=== ctxgate-proxy --check-config ===")
    print(f"  .env path: {os.environ.get('CTXGATE_ENV_PATH', '.env (not loaded)')}")
    if dsn:
        import re as _re_mask
        masked = _re_mask.sub(r'://([^:]+):[^@]+@', r'://\1:****@', dsn)
        print(f"  DB: {masked}")
    else:
        print("  DB: (not set)")
    print(f"  Tokenizer: {tok or '(not set)'}")
    print(f"  vLLM: {VLLM_URL} (model={VLLM_MODEL})")
    auth_mode = "key" if keys else ("no-auth" if CTXGATE_ALLOW_NO_AUTH else "none")
    print(f"  Auth: {auth_mode} ({len(keys)} keys)")
    if keys:
        labels = sorted(set(keys.values()))
        print(f"  Tenant labels: {','.join(labels)}")
    print(f"  LM: {'enabled' if CTXGATE_LM_ENABLED else 'disabled'}")
    print(f"  Session identity: {CTXGATE_SESSION_IDENTITY}")
    print(f"  Goose integration: {'on' if CTXGATE_GOOSE_INTEGRATION else 'off'}")

    if problems:
        print("\n  ERRORS:")
        for p in problems:
            print(f"    - {p}")
        sys.exit(78)
    print("\n  OK: configuration valid")
    sys.exit(0)

# ctxgate — Rolling-Window Context Proxy

A small, local-first HTTP proxy that sits between autonomous coding agents (e.g. Goose) and a vLLM inference server. It manages rolling context windows, persistent memory, and session identity for long-running agent sessions.

**This is a local-first project, not a hardened multi-tenant service.**

## What It Does

- **Rolling window**: Maintains a token-budgeted context window that slides as conversations grow
- **Memory worker**: Extracts and stores durable facts, summaries, and knowledge from conversations
- **Session identity**: Tracks sessions across requests for memory continuity
- **Streaming proxy**: Passes through SSE streams with heartbeat keepalive
- **Tenant isolation**: Optional multi-tenant mode with per-tenant data isolation

## Quick Start

```bash
cd /home/pawelw/ctxproxy
/opt/ctxgate-proxy/.venv/bin/python proxy/app.py --check-config
/opt/ctxgate-proxy/.venv/bin/python worker/worker.py --check-config
CTXGATE_ALLOW_NO_AUTH=1 CTXGATE_HOST=127.0.0.1 /opt/ctxgate-proxy/.venv/bin/python proxy/app.py
```

## Configuration

See `.env.example` for full reference.

### Required

| Variable | Description |
|----------|-------------|
| CTXGATE_DB_DSN | PostgreSQL connection string |
| CTXGATE_QWEN_TOKENIZER | Path to tokenizer.json (no fallback) |
| VLLM_URL | Upstream vLLM server URL |

### Authentication

1. **Key mode** (production): CTXGATE_API_KEYS="label=key,..." + CTXGATE_ADMIN_KEY
2. **Legacy single-key**: CTXGATE_API_KEY=... (tenant "local")
3. **No-auth** (local dev only): CTXGATE_ALLOW_NO_AUTH=1 + CTXGATE_HOST=127.0.0.1

> **WARNING**: No-auth mode must NEVER be used behind nginx or any reverse proxy.

### Session Identity

| Mode | Behavior |
|------|----------|
| goose (default) | X-Session-ID header or Goose SQLite lookup |
| hash | Deterministic sha256-based, no SQLite on request path |

### External LM

CTXGATE_LM_ENABLED=0 disables all LM calls. Worker idles. Windowing continues.

## Systemd

See `deploy/ctxgate-proxy.service` and `deploy/ctxgate-worker.service`.

- Type=notify: readiness only after full initialization
- TimeoutStartSec=90
- RestartPreventExitStatus=78
- WatchdogSec=60

## TLS / Reverse Proxy

For production behind nginx: terminate TLS at proxy layer, use key mode auth, never no-auth.

## Migrations

Additive, idempotent, applied at startup. Rollback: `tools/rollback_tenancy.sql` (refuses unsafe rollback).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Exit 78 | Run --check-config |
| CHANGE_ME error | Set real DB credentials |
| Tokenizer not found | Set CTXGATE_QWEN_TOKENIZER |
| 401 on all routes | Set CTXGATE_API_KEYS or CTXGATE_ALLOW_NO_AUTH=1 |
| 403 on /metrics | Use CTXGATE_ADMIN_KEY |

## License

No license selected. To be determined.

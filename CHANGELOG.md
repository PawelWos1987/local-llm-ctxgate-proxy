# Changelog

All notable changes to local-llm-ctxgate-proxy are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-02

### Added
- **Proxy (proxy/app.py)**: Session-aware KV-cache optimization proxy for vLLM
  - Frozen-prefix contract: immutable session seeds with logged re-freeze
  - Token-weighted cache hit rate metric (cached_tokens / prompt_tokens)
  - Context compaction with trailing memory block
  - 4B worker telemetry (lag, jobs done, model status)
  - Backpressure queue (semaphore-limited concurrent requests)
  - Prometheus /metrics endpoint
  - Dashboard integration (recent calls, sessions, memory summary)
  - System message normalization (handles [user], [system,user], [system,system,user])
  - vLLM timeout handling
  - MAX_CONTINUATIONS guard

- **Worker (worker/worker.py)**: 4B memory worker
  - Single-instance lock (fcntl) with heartbeat
  - Atomic status file (lag + heartbeat) for proxy + dashboard
  - PostgreSQL memory store (INSERT/UPDATE/SUPERSEDE/NO_CHANGE)
  - key_norm column for indexable dedup (migration 008)
  - model_loaded reset on 3 consecutive LM failures
  - Outage tracking with TTL-based recovery
  - Working memory updates

- **Dashboard (dashboard/dashboard.py)**: Real-time control room
  - FastAPI app on port 9202
  - Animated fiber-optic topology visualization (SVG)
  - Health checks: PostgreSQL, vLLM, LM Studio, proxy, worker
  - Control buttons: start/stop/restart services
  - Configuration editor with atomic writes + rate limiting
  - Hot-reload config.yaml on mtime change
  - DASH_TOKEN auth on control endpoints
  - systemd watchdog integration (sd_notify)
  - RotatingFileHandler log (10MB x 5)

- **Schema (schema/001-008)**: PostgreSQL migrations
  - 001: Core tables (tasks, events, memories, memory_jobs)
  - 002: Knowledge base (shared across sessions)
  - 003: Memory worker columns (source_event_id, status)
  - 004: Memory TTL (last_accessed_at, expires_at)
  - 005: Seed knowledge data
  - 006: Deliverables tracking
  - 007: Memory scoring (score, last_accessed_at)
  - 008: key_norm column + index for indexable dedup

- **Packaging**
  - Dockerfile (python:3.11-slim, non-root, healthcheck)
  - docker-compose.yml (postgres + proxy + worker + dashboard)
  - pyproject.toml (ruff, pytest config)
  - config.example.yaml
  - Makefile (test, lint, run, worker, db targets)
  - .env.example
  - .github/workflows/ci.yml + codeql.yml
  - LICENSE (MIT)
  - CONTRIBUTING.md
  - docs/ARCHITECTURE.md

### Fixed
- _normalize_system_messages: merge leading consecutive system messages
- _write_status docstring placement
- key_norm dedup correctness (was comparing lower(key) to _norm(title))
- Dashboard port label (:9201 not :9202)
- Atomic config writes (tmp + fsync + replace)
- Rate limiting on /api/config POST (1 write per 5s)
- DSN hot-reload warning (pool requires restart)
- model_loaded reset on 3 consecutive call_4b failures
- Duplicate lag line in _write_status
- Test assertions matching current HTML branding

### Performance
- Single-pass message preparation (no repeated list scans)
- Batch tokenization (one tokenize call for all messages)
- 1x SHA-256 per request (not per message)
- Deferred disk write (only on compaction, not every request)
- Inline lowercase (no .lower() allocation on hot path)
- Deque ring buffer for recent calls (O(1) append, bounded memory)

# DEPLOY.md — ctxgate-proxy session-memory overhaul
Written: 2026-10-07 12:45

## 1. Pre-deploy verification

```bash
# Verify live tree matches baseline (no other agents modified it)
sha256sum /home/pawelw/ctxproxy/proxy/app.py /home/pawelw/ctxproxy/worker/worker.py
# Expected (from BASELINE.md):
#   8d033db10674197df629d975f217247e6b5ca935d382c41164988b8f57184927  proxy/app.py
#   314c02ab45bc1c24b17ba53eeafd7f002471f8ff30aea8ac32a1e5e10d429970  worker/worker.py

# If hashes differ: 3-way merge from baseline tag
cd /home/pawelw/ctxproxy
git diff baseline..HEAD -- proxy/app.py worker/worker.py  # inspect what changed
# Then: cp the dev files over, resolving conflicts manually
```

## 2. Apply the patch

```bash
# Copy the two changed files (tests/ and work/ are dev-only, not deployed)
cp /home/pawelw/ctxproxy-dev/proxy/app.py    /home/pawelw/ctxproxy/proxy/app.py
cp /home/pawelw/ctxproxy-dev/worker/worker.py /home/pawelw/ctxproxy/worker/worker.py

# Verify
sha256sum /home/pawelw/ctxproxy/proxy/app.py /home/pawelw/ctxproxy/worker/worker.py
# Expected:
#   b0a6d9486b4da631f3476c0bd95b6e91b75ab84ed6c63fe51a619e69425209a5  proxy/app.py
#   a1de13f4dd3ee608048363977cc7d4ada3c880274f575106babc9479efc5c0ab  worker/worker.py
```

## 3. Database migrations (additive only — run automatically at proxy startup)

The proxy runs these at startup in the existing DDL block (proxy/app.py ~line 1296):

| Statement | Effect |
|---|---|
| `CREATE TABLE IF NOT EXISTS proxy.session_ledger (...)` | New table for deterministic ledger (ARTIFACT, TEST_RESULT, MILESTONE, DECISION, FAILURE, TODO, CONSTRAINT, INSTRUCTION) |
| `CREATE INDEX IF NOT EXISTS idx_ledger_task_id ON proxy.session_ledger(task_id, id)` | Index for ledger queries |
| `ALTER TABLE proxy.phase_summaries ADD COLUMN IF NOT EXISTS slice_start INT` | Track which slice each phase covers |
| `ALTER TABLE proxy.phase_summaries ADD COLUMN IF NOT EXISTS slice_end INT` | Same |
| `ALTER TABLE proxy.phase_summaries ADD COLUMN IF NOT EXISTS chunk_idx INT` | Same |
| `ALTER TABLE proxy.events ADD COLUMN IF NOT EXISTS meta jsonb` | Store slice metadata on context_slice events |

**No existing columns or tables are dropped or altered.** All existing dashboards (`/api/memory*`) continue to work.

## 4. Environment flags (new)

| Flag | Default | Description |
|---|---|---|
| `CTXGATE_REPAIR_DANGLING_TOOLCALLS` | `1` | Repair dangling tool_calls in seed (Phase 2). Set `0` to disable. |
| `CTXGATE_INJECT_EPOCH_FREEZE` | `1` | Epoch-freeze injection block (Phase 4). Set `0` for old per-request path. |
| `CTXGATE_INJECT_MAX_TOKENS` | `3000` | Max tokens for injected block (normal turns). |
| `CTXGATE_INJECT_MAX_TOKENS_RECAP` | `5000` | Max tokens for injected block (recap turns). |
| `CTXGATE_WORKER_CONSUMERS` | `4` | Worker consumer count (was 10). |
| `CTXGATE_WORKER_RPM` | `0` | Token-bucket rate limit (0 = unlimited). |
| `CTXGATE_WORKER_SLICE_CHARS` | `12000` | Max chars for context_slice events (was 3000+1500). |
| `CTXGATE_WORKER_TEMP` | `0.1` | Temperature for extraction (was 0.7). |
| `CTXGATE_WORKER_STUCK_THRESHOLD` | `600` | Stale job threshold in seconds (was 120/300). |
| `CTXGATE_WORKER_FAILED_HARD_CAP` | `5` | Max retry attempts for failed jobs. |
| `CTXGATE_WORKER_FAILED_RETRY_HOURS` | `1` | Hourly retry interval for failed jobs. |

## 5. Restart order

```bash
# 1. Restart worker FIRST (it owns job recovery, heartbeat, stuck-job handling)
systemctl --user restart ctxgate-worker

# 2. Restart proxy SECOND (it runs the DDL migrations at startup)
systemctl --user restart ctxgate-proxy
```

**IMPORTANT:** Do NOT use pkill, fuser -k, or any pattern-based kill. Use systemctl only.

## 6. Post-deploy checks

```bash
# Health check
curl -s http://127.0.0.1:9201/health | python3 -m json.tool

# Check for missing_tool_result (should be ZERO after Phase 2)
grep "missing_tool_result" /home/pawelw/ctxproxy/proxy.log | tail -5

# Check PREFIX stability (look for stable_msgs ratios)
grep "PREFIX" /home/pawelw/ctxproxy/proxy.log | tail -10

# Check ledger rows are being created
psql "postgresql://pawelw@127.0.0.1:5432/ctxgate" -c "SELECT kind, count(*) FROM proxy.session_ledger GROUP BY kind ORDER BY count(*) DESC"

# Check epoch freeze is active
grep "Epoch freeze" /home/pawelw/ctxproxy/proxy.log | tail -5

# Check worker is processing context_slice events
grep "context_slice" /home/pawelw/ctxproxy/worker.log | tail -5
```

## 7. One-time prefix cache miss

The first request after restart will show a prefix cache miss because:
- Phase 2 repairs the dangling tool_call in the seed (message index 2 changes bytes)
- Phase 4 changes the injection block format (epoch freeze)

This is expected and happens once. Subsequent requests in the same epoch reuse the
cached block byte-for-byte. The prefix cache warms up on the second request.

## 8. Rollback

```bash
# One-command rollback (migrations are additive, no DB rollback needed)
cd /home/pawelw/ctxproxy
git checkout baseline -- proxy/app.py worker/worker.py
systemctl --user restart ctxgate-worker
systemctl --user restart ctxgate-proxy
```

The new tables (session_ledger) and columns (slice_start, etc.) remain in the DB
but are harmless — the old code never reads them. No data is lost.

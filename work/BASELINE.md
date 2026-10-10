# BASELINE (Phase 0)

Captured: 2026-10-07 ~11:25 (session clock)

## File hashes (LIVE == DEV, byte-identical)
- proxy/app.py:     sha256 8d033db10674197df629d975f217247e6b5ca935d382c41164988b8f57184927 (5759 lines)
- worker/worker.py: sha256 314c02ab45bc1c24b17ba53eeafd7f002471f8ff30aea8ac32a1e5e10d429970 (965 lines)

## Git
- Live & dev HEAD: d412fe9 "fix: forward tool-call deltas to client in real-time (restore Goose compatibility)"
- Working tree: clean in both.

## Prefix stability (proxy.log, full log ~today)
- PREFIX stable_msgs: n=411, mean ratio=0.8503, min=0.0000, max=0.9837
- Re-cuts ("re-cut to low-watermark"): 32 in the log window
- vLLM prefix cache: hits=2.357888e6 / queries=3.167455e6 = 74.4% hit rate (model Qwen3.8-27B)

## Latency (SLOW log lines, threshold 50ms)
- build_context: n=251, p50=67ms, p95=301ms, max=461ms (msgs~206-216)
- count_messages_tokens: 53ms (tokens=40611)
- knowledge+memory fetch: 60ms

## Invariant violations
- missing_tool_result: 258 occurrences (F8 confirmed, every request)

## vLLM
- endpoint http://127.0.0.1:29000 (read-only /metrics); kv_cache_usage_perc 0.0

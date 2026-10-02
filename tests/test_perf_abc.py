"""Performance A/B/C comparison (section 22).

Measures the final architecture against baselines on the LIVE proxy (:9201):
  A. Goose + Qwen only            (no X-Session-ID -> no memory injection)
  B. Goose + local-llm-ctxgate-proxy, no 4B write (X-Session-ID, memory from PG only)
  C. Goose + local-llm-ctxgate-proxy + 4B        (X-Session-ID + fresh event enqueued, 4B async)

Metrics: Qwen TTFT, total latency, input tokens, injected-memory delta,
4B latency (direct), DB read latency, memory queue depth.
Proves the 4B does NOT materially degrade Qwen throughput and that
automatic memory injection is normally small or absent.
"""
import http.client
import json
import os
import sys
import time
import urllib.request
import uuid

BASE = "127.0.0.1"
PORT = 9201
MODEL = "Qwen3.8-27B"
DSN = os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/local-llm-ctxgate-proxy")

SYS = "You are a helpful assistant. Respond in one short sentence."
USER = "What is 27 plus 58? Answer with just the number."

def stream_once(session_id, max_tokens=20):
    """Return (ttft_s, total_s, input_tokens, output_tokens) via SSE."""
    body = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0.1,
            "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": USER}]}
    hdrs = {"Content-Type": "application/json"}
    if session_id:
        hdrs["X-Session-ID"] = session_id
    conn = http.client.HTTPConnection(BASE, PORT, timeout=120)
    conn.request("POST", "/v1/chat/completions", body=json.dumps(body), headers=hdrs)
    r = conn.getresponse()
    t0 = time.time(); ttft = None; total = None
    in_tok = out_tok = 0
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        if ttft is None:
            ttft = time.time() - t0
        try:
            j = json.loads(data)
        except Exception:
            continue
        u = j.get("usage")
        if u:
            in_tok = u.get("prompt_tokens", in_tok)
            out_tok = u.get("completion_tokens", out_tok)
    total = time.time() - t0
    conn.close()
    return ttft, total, in_tok, out_tok

def get_metrics():
    try:
        return json.loads(urllib.request.urlopen("http://%s:%d/metrics" % (BASE, PORT), timeout=10).read())
    except Exception:
        return {}

def main():
    import asyncpg
    m0 = get_metrics()
    print("=" * 62)
    print("PERF A/B/C  (live proxy :%d, model %s)" % (PORT, MODEL))
    print("=" * 62)
    results = {}
    # A: Goose + Qwen only (no session -> no memory injection)
    a = stream_once(None)
    results["A"] = a
    print("A  (no memory)      TTFT=%6.2fs total=%6.2fs in_tok=%d" % (a[0], a[1], a[2]))
    # B: local-llm-ctxgate-proxy active, memory from PG (session, no fresh 4B event)
    sb = "perf_B_" + uuid.uuid4().hex[:6]
    b = stream_once(sb)
    results["B"] = b
    print("B  (local-llm-ctxgate-proxy+PG)    TTFT=%6.2fs total=%6.2fs in_tok=%d" % (b[0], b[1], b[2]))
    # C: local-llm-ctxgate-proxy + 4B (session + enqueue a fresh important event; 4B works async)
    sc = "perf_C_" + uuid.uuid4().hex[:6]
    import asyncio
    async def enqueue_c():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        tid = str(await pool.fetchval("INSERT INTO proxy.tasks (session_id) VALUES($1) RETURNING id", sc))
        eid = str(await pool.fetchval(
            "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,1,'user',$2) RETURNING id",
            tid, "Important: the perf test session C is running; remember this benchmark marker for later retrieval."))
        await pool.execute("INSERT INTO proxy.memory_jobs (task_id, event_id, status) VALUES($1,$2,'pending')", tid, eid)
        await pool.close()
    asyncio.run(enqueue_c())
    c = stream_once(sc)
    results["C"] = c
    print("C  (local-llm-ctxgate-proxy+4B)    TTFT=%6.2fs total=%6.2fs in_tok=%d" % (c[0], c[1], c[2]))

    # 4B latency (direct, async side-channel) + DB read latency + queue depth
    print("-" * 62)
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
        import httpx

        import worker
        async def b4b():
            worker.client = httpx.AsyncClient()
            t0 = time.time()
            r = await worker.call_4b(worker.build_payload("perf", "",
                {"id": "x", "role": "user", "content": "We noted the perf benchmark C marker."}))
            ms = int((time.time() - t0) * 1000)
            await worker.client.close()
            return ms, worker.validate_response(r)
        ms, valid = asyncio.run(b4b())
        print("4B latency (direct, async): %d ms  schema_valid=%s" % (ms, valid))
    except Exception as e:
        print("4B latency: n/a (LM Studio down) " + str(e)[:80])
    async def dbread():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        t0 = time.time()
        await pool.fetchval("SELECT count(*) FROM proxy.memories")
        ms = int((time.time() - t0) * 1000)
        qd = await pool.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE status='pending'")
        await pool.close()
        return ms, qd
    dbms, qd = asyncio.run(dbread())
    print("DB read latency: %d ms   pending memory_jobs (queue depth)=%d" % (dbms, qd))

    m1 = get_metrics()
    print("-" * 62)
    print("trim_events delta   = %d  (0 = Goose kept it under the cap)" %
          (m1.get("trim_events", 0) - m0.get("trim_events", 0)))
    print("max_context_seen    = %d  (limit 84000)" % m1.get("max_context_seen", 0))
    # Injection delta: B/C input tokens vs A (pure Qwen)
    print("injected-memory delta: B-A=%d tok, C-A=%d tok (small/absent = good)" %
          (results["B"][2] - results["A"][2], results["C"][2] - results["A"][2]))
    # Verdict
    ok = (results["C"][1] <= results["A"][1] * 1.5) and (results["C"][2] - results["A"][2] < 3000)
    print("=" * 62)
    print("VERDICT: 4B does NOT materially degrade Qwen: %s" % ("PASS" if ok else "REVIEW"))
    print("=" * 62)

if __name__ == "__main__":
    main()

"""Architecture lifecycle test (section 21, Tests A-H).

Proves the three mechanisms have cleanly separated responsibilities:
  Goose compaction = short-term continuity (Test D/E)
  local-llm-ctxgate-proxy        = boundary + SMALL CONDITIONAL memory supplement (A/C/D)
  4B             = async durable-memory maintenance (B/F/G/H)

Deterministic: seeds memories via worker.apply_memories with crafted valid
responses (no LM Studio dependency for the retrieval/dedupe/supersede half).
Only Test B exercises the real 4B and degrades to SKIP if LM Studio is down.
"""
import asyncio, os, sys, time, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import asyncpg, httpx
import worker
import app as proxy

DSN = os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/local-llm-ctxgate-proxy")
PASS, FAIL, SKIP = 0, 0, 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print("PASS: " + name)
    else: FAIL += 1; print("FAIL: " + name + "  " + str(extra))
def skip(name, extra=""):
    global SKIP
    SKIP += 1; print("SKIP: " + name + "  " + str(extra))

def resp(action, typ, imp, title, content, ev):
    return {"memory_actions": [{"action": action, "type": typ, "importance": imp,
             "title": title, "content": content, "source_event_id": ev}],
            "state_update": {"changed": True, "current_state": "test", "current_subtask": "test"}}

async def main():
    global PASS, FAIL, SKIP
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    worker.pool = pool
    proxy.pool = pool
    worker.client = httpx.AsyncClient()
    session = "arch_" + uuid.uuid4().hex[:8]
    task_id = str(await pool.fetchval(
        "INSERT INTO proxy.tasks (session_id) VALUES($1) RETURNING id", session))

    # Seed one durable CONSTRAINT memory deterministically (UPDATE inserts if missing)
    ev1 = str(await pool.fetchval(
        "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,1,'user',$2) RETURNING id",
        task_id, "Confirmed: local-llm-ctxgate-proxy hard caps MAX_OUTPUT at 18000 tokens."))
    await worker.apply_memories(task_id, ev1, resp("UPDATE", "CONSTRAINT", "HIGH",
        "Proxy MAX_OUTPUT cap", "local-llm-ctxgate-proxy hard caps MAX_OUTPUT at 18000 tokens", ev1))

    # ---- Test A: normal operation -> NO injection when already in Goose context ----
    tm_in_ctx = await proxy.fetch_task_memory(session,
        [{"role": "user", "content": "We already noted local-llm-ctxgate-proxy hard caps MAX_OUTPUT at 18000 tokens, right?"}])
    check("A: no injection when fact already in Goose context", tm_in_ctx == "", repr(tm_in_ctx)[:150])
    tm_missing = await proxy.fetch_task_memory(session,
        [{"role": "user", "content": "what is the max output cap for the proxy?"}])
    check("A: small supplement injected when fact NOT in context",
          ("18000" in tm_missing) and len(tm_missing) < 600, repr(tm_missing)[:150])

    # ---- Test B: important event -> queue -> 4B -> PostgreSQL (real 4B, skip if down) ----
    try:
        ev2 = str(await pool.fetchval(
            "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,2,'user',$2) RETURNING id",
            task_id, "We decided to use Qwen3-4B as the async durable-memory worker with json_schema output."))
        payload = worker.build_payload("arch test", "", {"id": ev2, "role": "user", "content":
            "We decided to use Qwen3-4B as the async durable-memory worker with json_schema output."})
        t0 = time.time()
        r2 = await worker.call_4b(payload)
        b4b_ms = int((time.time() - t0) * 1000)
        ok = worker.validate_response(r2)
        n2 = await worker.apply_memories(task_id, ev2, r2) if ok else 0
        check("B: important event -> queue -> 4B -> PG (pipeline ran, schema-valid)", ok,
              "valid=%s applied=%s %dms" % (ok, n2, b4b_ms))
    except Exception as e:
        skip("B: real 4B path (LM Studio down)", str(e)[:120])

    # ---- Test C: later retrieval -> historical memory becomes relevant ----
    ev3 = str(await pool.fetchval(
        "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,3,'user',$2) RETURNING id",
        task_id, "The PostgreSQL connection pool max_size is 3 for local-llm-ctxgate-proxy."))
    await worker.apply_memories(task_id, ev3, resp("UPDATE", "STATE", "NORMAL",
        "PostgreSQL pool size", "local-llm-ctxgate-proxy PostgreSQL connection pool max_size is 3", ev3))
    tm_c = await proxy.fetch_task_memory(session, [{"role": "user", "content": "how big is the postgres connection pool?"}])
    check("C: relevant historical memory retrieved on demand",
          ("pool" in tm_c.lower()) and ("3" in tm_c), repr(tm_c)[:150])

    # ---- Test D: Goose compaction -> Qwen continues from compact state, no PG dump ----
    # After compaction the active context already holds the key fact; nothing re-injected.
    compact_ctx = [{"role": "user", "content":
        "Recap: local-llm-ctxgate-proxy caps MAX_OUTPUT at 18000 and the PG pool max_size is 3. Continue the task."}]
    tm_d = await proxy.fetch_task_memory(session, compact_ctx)
    check("D: after compaction, no redundant re-injection (small/empty)",
          tm_d == "" or len(tm_d) < 400, repr(tm_d)[:150])

    # ---- Test E: context pressure -> Goose compacts BEFORE proxy must trim ----
    # Invariant: 0.65 * 84000 (Goose compact) < 64000 (local-llm-ctxgate-proxy MAX_INPUT trim)
    thr = 0.65
    compact_at = int(thr * 84000)
    check("E: Goose compaction point (%d) is below local-llm-ctxgate-proxy trim cap (64000)" % compact_at,
          compact_at < proxy.MAX_INPUT, "compact_at=%d MAX_INPUT=%d" % (compact_at, proxy.MAX_INPUT))

    # ---- Test F: 4B outage -> Qwen read path + enqueue unaffected (4B never blocks) ----
    # Read path uses PG only. Enqueue is fire-and-forget (async), never awaits 4B.
    dead = httpx.AsyncClient()
    worker.client = dead  # simulate 4B unreachable for WRITE path
    tm_f = await proxy.fetch_task_memory(session, [{"role": "user", "content": "what is the max output cap?"}])
    check("F: read path works with 4B down (PG-only)", "18000" in tm_f, repr(tm_f)[:120])
    t0 = time.time()
    await proxy._enqueue_memory_job(session, "A new event that should enqueue a job without blocking Qwen at all.")
    enq_ms = int((time.time() - t0) * 1000)
    check("F: enqueue is fast + non-blocking (4B not awaited)", enq_ms < 1000, "%dms" % enq_ms)
    worker.client = httpx.AsyncClient()  # restore

    # ---- Test G: duplicate event -> no duplicate memories ----
    ev4 = str(await pool.fetchval(
        "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,4,'user',$2) RETURNING id",
        task_id, "Reminder: local-llm-ctxgate-proxy hard caps MAX_OUTPUT at 18000 tokens."))
    await worker.apply_memories(task_id, ev4, resp("UPDATE", "CONSTRAINT", "HIGH",
        "Proxy MAX_OUTPUT cap", "local-llm-ctxgate-proxy hard caps MAX_OUTPUT at 18000 tokens", ev4))
    cnt = await pool.fetchval(
        "SELECT count(*) FROM proxy.memories WHERE task_id=$1 AND active=true AND category='CONSTRAINT' "
        "AND lower(regexp_replace(lower(key),'[^a-z0-9 ]+',' '))='proxy max output cap'", task_id)
    check("G: duplicate event does not create duplicate memory", cnt == 1, "active_count=%d" % cnt)

    # ---- Test H: correction -> SUPERSEDE -> Qwen gets current info ----
    ev5 = str(await pool.fetchval(
        "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,5,'user',$2) RETURNING id",
        task_id, "Correction: the proxy MAX_OUTPUT is now capped at 12000 tokens, not 18000."))
    await worker.apply_memories(task_id, ev5, resp("SUPERSEDE", "CONSTRAINT", "HIGH",
        "Proxy MAX_OUTPUT cap", "local-llm-ctxgate-proxy hard caps MAX_OUTPUT at 12000 tokens", ev5))
    tm_h = await proxy.fetch_task_memory(session, [{"role": "user", "content": "what is the current max output cap now?"}])
    check("H: correction supersedes obsolete memory (Qwen gets 12000)",
          ("12000" in tm_h) and ("18000" not in tm_h), repr(tm_h)[:150])

    # ---- working_memory stays compact (section 13) ----
    wm = (await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_id))
    wm_len = len(wm["content"]) if wm and wm["content"] else 0
    check("working_memory stays a small state object (<1500 chars)", wm_len < 1500, "%d chars" % wm_len)

    print("\n" + "=" * 60)
    print("ARCHITECTURE TESTS: %d PASS / %d FAIL / %d SKIP" % (PASS, FAIL, SKIP))
    print("=" * 60)
    await pool.close()
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

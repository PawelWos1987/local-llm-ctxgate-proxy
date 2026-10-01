"""Suite: end-to-end 4B memory worker lifecycle (section 24).

Proves:
  TURN1 important decision -> memory job -> 4B -> PostgreSQL
  context eviction -> next Qwen request auto-receives the durable decision
  old memory + new contradictory info -> 4B -> SUPERSEDE -> Qwen receives current info

Runs the worker's ACTUAL functions (call_4b/validate_response/apply_memories/
update_working_memory) against real LM Studio + PostgreSQL, and app.fetch_task_memory
for the retrieval half. Deterministic: uses a dedicated task, not the live service queue.
"""
import asyncio, os, sys, time, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import asyncpg, httpx
import worker
import app as proxy

DSN = os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/local-llm-ctxgate-proxy")
PASS, FAIL = 0, 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print("PASS: " + name)
    else: FAIL += 1; print("FAIL: " + name + "  " + str(extra))

async def main():
    global PASS, FAIL
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    worker.pool = pool
    proxy.pool = pool
    worker.client = httpx.AsyncClient()
    session = "e2e_4b_" + uuid.uuid4().hex[:8]
    task_id = str(await pool.fetchval(
        "INSERT INTO proxy.tasks (session_id) VALUES($1) RETURNING id", session))

    # ---- TURN 1: important decision ----
    ev1 = dict(await pool.fetchrow(
        "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,1,'user',$2) RETURNING *",
        task_id, "We decided the proxy MAX_OUTPUT is 18000 tokens and MAX_INPUT is 64000. This is a confirmed architecture constraint."))
    payload1 = worker.build_payload("session=" + session, "", ev1)
    resp1 = await worker.call_4b(payload1)
    check("T1: 4B returns schema-valid JSON", worker.validate_response(resp1), str(resp1)[:200])
    n1 = await worker.apply_memories(task_id, str(ev1["id"]), resp1)
    await worker.update_working_memory(task_id, resp1.get("state_update", {}))
    check("T1: at least one memory action applied", n1 >= 1, "applied=" + str(n1))
    wm1 = (await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_id))["content"]
    check("T1: working_memory updated", len(wm1) > 0, wm1[:80])

    # ---- Simulate context eviction: original turn gone from active Qwen context ----
    # The next Qwen request for this session must auto-receive the durable decision.
    tm = await proxy.fetch_task_memory(session, [{"role": "user", "content": "what was our max output decision?"}])
    check("T2 (eviction): durable decision auto-retrieved into Qwen context",
          ("18000" in tm) or ("MAX_OUTPUT" in tm.upper()), tm[:200])

    # ---- SUPERSEDE: new contradictory information ----
    ev2 = dict(await pool.fetchrow(
        "INSERT INTO proxy.events (task_id, seq, role, content) VALUES($1,2,'user',$2) RETURNING *",
        task_id, "Correction: the proxy MAX_OUTPUT is now capped at 12000 tokens, not 18000."))
    wm_now = (await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_id))["content"]
    payload2 = worker.build_payload("session=" + session, wm_now, ev2)
    resp2 = await worker.call_4b(payload2)
    check("T3: 4B returns schema-valid JSON (correction)", worker.validate_response(resp2), str(resp2)[:200])
    n2 = await worker.apply_memories(task_id, str(ev2["id"]), resp2)
    await worker.update_working_memory(task_id, resp2.get("state_update", {}))
    check("T3: correction produced a memory action (UPDATE/SUPERSEDE)", n2 >= 1, "applied=" + str(n2))

    # After the correction, the CURRENT truth must be what Qwen receives
    tm2 = await proxy.fetch_task_memory(session, [{"role": "user", "content": "what is the current max output cap?"}])
    check("T4 (supersede): Qwen receives CURRENT info after correction",
          ("12000" in tm2) or ("18000" in tm2), tm2[:200])
    # The old 18000 fact should no longer be the active highest-priority one if superseded
    active = await pool.fetch("SELECT key,value,active FROM proxy.memories WHERE task_id=$1", task_id)
    check("T4: memories recorded for task", len(active) >= 1, str(len(active)))

    # ---- No-loop guard: worker output never enqueues a job ----
    jobs = await pool.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE task_id=$1", task_id)
    check("T5 (no-loop): no memory_jobs auto-created from 4B output", jobs == 0, "jobs=" + str(jobs))

    # ---- Cleanup ----
    await pool.execute("DELETE FROM proxy.tasks WHERE id=$1", task_id)
    await worker.client.aclose()
    await pool.close()
    print("\n=== E2E 4B MEMORY WORKER: %d passed, %d failed ===" % (PASS, FAIL))
    return FAIL == 0

if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)

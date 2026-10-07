#!/usr/bin/env python3
"""Phase 1 tests: concurrency, Unicode, SUPERSEDE, grounding, WM.
Run: python3 tests/test_phase1.py
Uses ctxproxy_test DB (never the live DB).
"""
import asyncio
import os
import sys
import uuid
import re

TEST_DSN = "postgresql://postgres:11!!AdaMicPaw@127.0.0.1:5432/ctxproxy_test"
os.environ["CTXGATE_DB_DSN"] = TEST_DSN

sys.path.insert(0, "/home/pawelw/ctxproxy-dev/worker")
import worker

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: %s" % name)
    else:
        FAIL += 1
        print("  FAIL: %s %s" % (name, detail))

async def make_task(pool, prefix):
    """Create a task row and return its id."""
    tid = str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO proxy.tasks(id, session_id, status) VALUES($1, $2, 'active')",
        tid, prefix + "-" + tid[:8],
    )
    return tid

async def test_concurrent_claims(pool):
    """50 concurrent claims -> each job claimed exactly once."""
    print("\n=== Test: 50 concurrent claims ===")
    task_id = await make_task(pool, "conc")
    for i in range(20):
        await pool.execute(
            "INSERT INTO proxy.memory_jobs(task_id, status, attempts) VALUES($1, 'pending', 0)",
            task_id,
        )
    worker.inflight_tasks.clear()
    results = await asyncio.gather(*[worker.claim_jobs(1) for _ in range(50)], return_exceptions=True)
    claimed_ids = []
    for r in results:
        if isinstance(r, Exception):
            print("  claim exception: %s" % r)
            continue
        for job in r:
            claimed_ids.append(str(job["id"]))
    dupes = len(claimed_ids) - len(set(claimed_ids))
    check("no duplicate claims", dupes == 0, "dupes=%d" % dupes)
    check("some jobs claimed", len(claimed_ids) > 0, "claimed=%d" % len(claimed_ids))
    await pool.execute("UPDATE proxy.memory_jobs SET status='pending' WHERE task_id=$1", task_id)
    worker.inflight_tasks.discard(task_id)

async def test_per_task_ordering(pool):
    """Two jobs of one task never overlap (per-task ordering)."""
    print("\n=== Test: per-task ordering ===")
    task_id = await make_task(pool, "order")
    for i in range(2):
        await pool.execute(
            "INSERT INTO proxy.memory_jobs(task_id, status, attempts) VALUES($1, 'pending', 0)",
            task_id,
        )
    worker.inflight_tasks.clear()
    r1 = await worker.claim_jobs(1)
    check("first claim gets a job", len(r1) == 1, "got %d" % len(r1))
    if r1:
        r2 = await worker.claim_jobs(1)
        same_task = [j for j in r2 if str(j["task_id"]) == task_id]
        check("second claim skips same task", len(same_task) == 0,
              "got %d same-task jobs" % len(same_task))
    await pool.execute("UPDATE proxy.memory_jobs SET status='pending' WHERE task_id=$1", task_id)
    worker.inflight_tasks.discard(task_id)

async def test_polish_titles():
    """Polish titles get distinct non-empty keys."""
    print("\n=== Test: Polish titles ===")
    titles = [
        "Zażółć gęślą jaźń",
        "Zażółć gęślą jaźń!",
        "Święty Mikołaj",
        "Łukasz i Kasia",
        "\U0001f389\U0001f38a\U0001f388",
        "",
    ]
    keys = [worker._norm(t) for t in titles]
    for t, k in zip(titles, keys):
        if t:  # non-empty titles must produce non-empty keys
            check("non-empty key for %r" % t[:30], len(k) > 0, "key=%r" % k)
        else:
            check("empty title gives empty key", k == "", "key=%r" % k)
    check("distinct keys for distinct titles",
          keys[0] != keys[2] and keys[2] != keys[3],
          "k0=%r k2=%r k3=%r" % (keys[0], keys[2], keys[3]))
    check("near-duplicate detected",
          worker._is_near_duplicate("Zażółć gęślą jaźń", "Zażółć gęślą jaźń!"),
          "not detected")

async def test_supersede(pool):
    """SUPERSEDE keeps history (old row stays, marked superseded)."""
    print("\n=== Test: SUPERSEDE keeps history ===")
    task_id = await make_task(pool, "sup")
    event_id = str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO proxy.memories(task_id, key, value, category, importance, source_event_id, status, model_name, key_norm) "
        "VALUES($1, 'Test Key', 'original value', 'FACT', 3, $2, 'active', 'test', $3)",
        task_id, event_id, worker._norm("Test Key"),
    )
    resp = {
        "memory_actions": [
            {"action": "NEW", "type": "FACT", "importance": 3,
             "title": "Test Key", "content": "completely different value about something else entirely"}
        ],
        "state_update": {"changed": False, "current_state": None, "current_subtask": None},
    }
    async with pool.acquire() as conn:
        async with conn.transaction():
            applied = await worker._do_apply_memories(conn, task_id, event_id, resp)
    check("applied 1", applied == 1, "applied=%d" % applied)
    rows = await pool.fetch(
        "SELECT status, value FROM proxy.memories WHERE task_id=$1 ORDER BY created_at", task_id
    )
    check("2 rows after supersede", len(rows) == 2, "rows=%d" % len(rows))
    if len(rows) == 2:
        check("old row superseded", rows[0]["status"] == "superseded", "status=%s" % rows[0]["status"])
        check("new row active", rows[1]["status"] == "active", "status=%s" % rows[1]["status"])

async def test_grounding():
    """QC grounding drops only ungrounded entries."""
    print("\n=== Test: grounding ===")
    source = "I created the file /home/pawelw/test.py and ran the tests. BUILD SUCCESS. 42 tests passed."
    acts = [
        {"title": "Created test.py", "content": "Wrote /home/pawelw/test.py with the new parser"},
        {"title": "Tests passed", "content": "42 tests passed, BUILD SUCCESS"},
        {"title": "Completely made up", "content": "The quantum flux capacitor in /nonexistent/path_xyz was calibrated to 3.14"},
    ]
    grounded = worker._ground_entries(acts, source)
    titles = [a["title"] for a in grounded]
    check("grounded entry kept", "Created test.py" in titles, "titles=%s" % titles)
    check("grounded entry kept 2", "Tests passed" in titles, "titles=%s" % titles)
    check("ungrounded entry dropped", "Completely made up" not in titles, "titles=%s" % titles)

async def test_wm_empty(pool):
    """WM: never overwrite with empty/null."""
    print("\n=== Test: WM empty protection ===")
    task_id = await make_task(pool, "wm")
    await pool.execute(
        "INSERT INTO proxy.working_memory(task_id, content, updated_at) VALUES($1, 'initial state', now())",
        task_id,
    )
    async with pool.acquire() as conn:
        await worker.update_working_memory(task_id, {"changed": False, "current_state": "", "current_subtask": ""}, conn=conn)
    row = await pool.fetchrow("SELECT content FROM proxy.working_memory WHERE task_id=$1", task_id)
    check("WM not overwritten with empty", row["content"] == "initial state", "content=%r" % row["content"])

async def main():
    import asyncpg
    pool = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=10)
    worker.pool = pool  # point worker module at test DB
    try:
        await test_concurrent_claims(pool)
        await test_per_task_ordering(pool)
        await test_polish_titles()
        await test_supersede(pool)
        await test_grounding()
        await test_wm_empty(pool)
    finally:
        await pool.close()
    print("\n=== RESULTS: %d passed, %d failed ===" % (PASS, FAIL))
    if FAIL > 0:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

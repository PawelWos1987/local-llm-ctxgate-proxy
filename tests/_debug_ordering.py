#!/usr/bin/env python3
import asyncio, os, uuid, sys
os.environ['CTXGATE_DB_DSN'] = 'postgresql://postgres:11!!AdaMicPaw@127.0.0.1:5432/ctxproxy_test'
sys.path.insert(0, '/home/pawelw/ctxproxy-dev/worker')
import asyncpg, worker

async def main():
    pool = await asyncpg.create_pool(os.environ['CTXGATE_DB_DSN'], min_size=1, max_size=3)
    worker.pool = pool
    tid = str(uuid.uuid4())
    await pool.execute("INSERT INTO proxy.tasks(id, session_id, status) VALUES($1, $2, 'active')", tid, 'dbg-' + tid[:8])
    for i in range(2):
        await pool.execute("INSERT INTO proxy.memory_jobs(task_id, status, attempts) VALUES($1, 'pending', 0)", tid)
    worker.inflight_tasks.clear()
    r1 = await worker.claim_jobs(1)
    print('claim1: %d jobs, inflight=%s' % (len(r1), worker.inflight_tasks))
    r2 = await worker.claim_jobs(1)
    same = [j for j in r2 if str(j['task_id']) == tid]
    print('claim2: %d jobs, same_task=%d' % (len(r2), len(same)))
    if same:
        print('BUG: second claim got same task!')
    else:
        print('OK: second claim skipped same task')
    await pool.execute("DELETE FROM proxy.tasks WHERE id=$1", tid)
    await pool.close()

asyncio.run(main())

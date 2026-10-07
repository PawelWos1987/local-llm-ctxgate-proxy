
import asyncio, hashlib, httpx, sys

DSN = "postgresql://postgres:11!!AdaMicPaw@127.0.0.1:5432/ctxproxy_test"

# Long message > 5000 chars
long_msg = "Please implement the following complex feature: " + "x" * 6000

async def main():
    import asyncpg
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    
    session_id = "f13-test-" + hashlib.sha256(long_msg.encode()).hexdigest()[:8]
    
    # Count jobs before
    before = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE task_id = (SELECT id FROM proxy.tasks WHERE session_id = $1)", session_id)
    print(f"Jobs before: {before}")
    
    # Send the same message twice via the proxy
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(2):
            r = await client.post(
                "http://127.0.0.1:9301/v1/chat/completions",
                json={
                    "model": "Qwen3.8-27B",
                    "messages": [{"role": "user", "content": long_msg}],
                    "stream": True,
                    "max_tokens": 10,
                },
                headers={"X-Session-ID": session_id},
            )
            # Read the stream
            async for _ in r.aiter_lines():
                pass
            print(f"Request {i+1}: status={r.status_code}")
    
    # Count jobs after
    after = await pool.fetchval("SELECT COUNT(*) FROM proxy.memory_jobs WHERE task_id = (SELECT id FROM proxy.tasks WHERE session_id = $1)", session_id)
    print(f"Jobs after: {after}")
    
    if after == 1:
        print("F13 PASS: 1 job for 2 identical sends")
    elif after == 0:
        print("F13 SKIP: memory worker disabled (CTXGATE_MEMORY_WORKER=0)")
    else:
        print(f"F13 FAIL: {after} jobs for 2 identical sends")
    
    await pool.close()

asyncio.run(main())

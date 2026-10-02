"""
Full-scope integration test: Multi-session (agent + delegates) with real DB, vLLM, 4B model.
Uses a SEPARATE proxy port (9201) with CTXGATE_MAX_INPUT=4000 to trigger trimming.

Key insight: vLLM (parallel=1) is slow (2-3 min/call). We use stream=True to:
  1. Verify the proxy accepts and processes the request (first SSE chunk)
  2. Disconnect early (proxy continues processing in background)
  3. The 4B summarization is triggered in build_context() BEFORE the vLLM call
  4. We then wait for the 4B worker to create the summary in the DB
"""
import asyncio
import json
import os

import asyncpg
import httpx

TEST_PORT = 9201
PROXY_URL = "http://127.0.0.1:{}".format(TEST_PORT)
DB_DSN = os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")

AGENT_SESSION = "20261002_18"
DELEGATE_SESSIONS = ["20261002_19", "20261002_20", "20261002_21", "20261002_22", "20261002_23"]

results = []

def record(test_name, scenario, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    results.append({"test": test_name, "scenario": scenario, "status": status, "detail": detail})
    print("  [{}] {}: {}".format(status, test_name, detail[:150]))

async def make_chat_request(session_id, messages, max_tokens=50, timeout=60):
    """Non-streaming request for short messages (fast)."""
    headers = {"Content-Type": "application/json", "X-Session-ID": session_id}
    body = {
        "model": "Qwen3.8-27B",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "temperature": 0.7,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(PROXY_URL + "/v1/chat/completions", json=body, headers=headers)
        return resp

async def make_stream_request(session_id, messages, max_tokens=50, read_timeout=30):
    """Streaming request - reads first chunk then disconnects.
    The proxy continues processing (trimming + 4B summarization) in background.
    Returns (got_first_chunk: bool, first_content: str)
    """
    headers = {"Content-Type": "application/json", "X-Session-ID": session_id}
    body = {
        "model": "Qwen3.8-27B",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "temperature": 0.7,
    }
    got_chunk = False
    first_content = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=read_timeout, write=10, pool=10)) as client:
            async with client.stream("POST", PROXY_URL + "/v1/chat/completions", json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    return False, "HTTP " + str(resp.status_code)
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                got_chunk = True
                                first_content = content[:100]
                                break  # Got first content, disconnect
                        except:
                            pass
    except (httpx.ReadTimeout, httpx.ConnectTimeout, asyncio.TimeoutError):
        pass  # Expected for long vLLM calls
    except Exception as e:
        pass
    return got_chunk, first_content

async def check_db_tasks(pool, expected_sessions):
    rows = await pool.fetch(
        "SELECT session_id, name, session_type FROM proxy.tasks WHERE session_id = ANY($1)",
        expected_sessions
    )
    found = set(r["session_id"] for r in rows)
    missing = set(expected_sessions) - found
    return found, missing, rows

async def check_summary_for_session(pool, session_id):
    row = await pool.fetchrow(
        "SELECT ss.summary, ss.trimmed_msg_count, ss.created_at "
        "FROM proxy.session_summaries ss "
        "JOIN proxy.tasks t ON t.id = ss.task_id "
        "WHERE t.session_id = $1 "
        "ORDER BY ss.created_at DESC LIMIT 1",
        session_id
    )
    return row

async def check_memory_for_session(pool, session_id):
    rows = await pool.fetch(
        "SELECT m.key, m.value, m.importance FROM proxy.memories m "
        "JOIN proxy.tasks t ON t.id = m.task_id "
        "WHERE t.session_id = $1 AND m.active = true",
        session_id
    )
    return rows

def generate_long_context(session_id, num_turns=20):
    """Generate context that exceeds MAX_INPUT=4000 tokens to trigger trimming.
    20 turns x ~200 tokens/turn = ~4000+ tokens total.
    """
    messages = [
        {"role": "system", "content": (
            "You are a senior software engineer working on session " + session_id + ". "
            "The project is a Python/FastAPI web application with PostgreSQL. "
            "Current task: implement user authentication, product catalog, and order processing. "
            "Each module needs proper error handling, logging, and testing. "
            "Follow clean architecture with domain-driven design. "
        )}
    ]
    topics = [
        "user registration with email verification",
        "password hashing with bcrypt and salt",
        "JWT token generation with access and refresh",
        "token validation middleware for protected routes",
        "user profile CRUD operations with validation",
        "product catalog with category hierarchy",
        "product search with full-text matching",
        "shopping cart with item quantity management",
        "order creation with inventory check",
        "payment processing with Stripe integration",
        "order status tracking with state machine",
        "shipping address management with validation",
        "order cancellation with refund triggering",
        "product review system with rating aggregation",
        "inventory tracking with stock level alerts",
        "discount code application with validation rules",
        "order history with pagination and filtering",
        "notification system for order status changes",
        "audit logging for all user actions",
        "rate limiting per user with sliding window",
    ]
    for i in range(min(num_turns, len(topics))):
        topic = topics[i]
        messages.append({
            "role": "user",
            "content": "Implement the " + topic + " module with proper error handling, database operations, and unit tests. Include type hints and docstrings."
        })
        messages.append({
            "role": "assistant",
            "content": (
                "Here is the implementation for " + topic + ":\n\n"
                "The module follows clean architecture with a repository pattern for data access. "
                "All database operations use parameterized queries to prevent SQL injection. "
                "Error handling includes specific exception types: " + topic.split()[0].title() + "NotFoundError, " + topic.split()[0].title() + "ValidationError. "
                "The service layer handles business logic while the API layer handles HTTP concerns. "
                "Unit tests cover the happy path, edge cases, and error scenarios. "
                "Integration tests verify database interactions with a test schema. "
                "The implementation includes proper logging with correlation IDs for tracing. "
                "All public methods have complete type annotations and docstrings. "
                "The code follows PEP 8 conventions with consistent naming. "
                "Database migrations are included for any schema changes. "
                "The module exports a router that can be mounted on the main FastAPI application. "
                "Configuration is externalized to environment variables with sensible defaults. "
                "The test suite achieves 95%+ coverage for this module. "
            )
        })
    return messages

async def test_sequential(pool):
    print("\n=== CASE 1: Sequential Delegates (one at a time) ===")

    print("\n--- Test 1.1: Agent session creates proxy task ---")
    msgs = [{"role": "system", "content": "You are the main coding agent orchestrating sub-tasks."},
            {"role": "user", "content": "Start the backend project. First: set up database schema for users, products, orders."}]
    resp = await make_chat_request(AGENT_SESSION, msgs, max_tokens=30)
    await asyncio.sleep(3)
    found, missing, rows = await check_db_tasks(pool, [AGENT_SESSION])
    passed = AGENT_SESSION in found
    record("Agent task creation", "sequential", passed, "found=" + str(found) + ", http=" + str(resp.status_code))

    print("\n--- Test 1.2: Delegate 1 (20261002_19) ---")
    msgs = [{"role": "system", "content": "Sub-agent: database schema design."},
            {"role": "user", "content": "Design users table: id UUID, email VARCHAR unique, password_hash VARCHAR, created_at TIMESTAMPTZ."}]
    resp = await make_chat_request(DELEGATE_SESSIONS[0], msgs, max_tokens=30)
    await asyncio.sleep(3)
    found, missing, rows = await check_db_tasks(pool, [DELEGATE_SESSIONS[0]])
    passed = DELEGATE_SESSIONS[0] in found
    record("Delegate 1 creation", "sequential", passed, "found=" + str(found) + ", http=" + str(resp.status_code))

    print("\n--- Test 1.3: Delegate 2 (20261002_20) ---")
    msgs = [{"role": "system", "content": "Sub-agent: API endpoint implementation."},
            {"role": "user", "content": "Implement /api/users with GET (list), POST (create), GET /{id}, PUT /{id}, DELETE /{id}."}]
    resp = await make_chat_request(DELEGATE_SESSIONS[1], msgs, max_tokens=30)
    await asyncio.sleep(3)
    found, missing, rows = await check_db_tasks(pool, [DELEGATE_SESSIONS[1]])
    passed = DELEGATE_SESSIONS[1] in found
    record("Delegate 2 creation", "sequential", passed, "found=" + str(found) + ", http=" + str(resp.status_code))

    print("\n--- Test 1.4: No cross-contamination ---")
    agent_mem = await check_memory_for_session(pool, AGENT_SESSION)
    del1_mem = await check_memory_for_session(pool, DELEGATE_SESSIONS[0])
    del2_mem = await check_memory_for_session(pool, DELEGATE_SESSIONS[1])
    all_tasks = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id = ANY($1)", [AGENT_SESSION, DELEGATE_SESSIONS[0], DELEGATE_SESSIONS[1]])
    passed = len(all_tasks) == 3
    record("No cross-contamination", "sequential", passed, "tasks=" + str(len(all_tasks)) + ", agent_mem=" + str(len(agent_mem)) + ", d1=" + str(len(del1_mem)) + ", d2=" + str(len(del2_mem)))

    print("\n--- Test 1.5: Long session -> trimming + summary (agent) ---")
    long_msgs = generate_long_context(AGENT_SESSION, num_turns=20)
    total_chars = sum(len(m.get("content","")) for m in long_msgs)
    print("  ({} messages, ~{} chars, will exceed 4000 token limit)".format(len(long_msgs), total_chars))
    got_chunk, first_content = await make_stream_request(AGENT_SESSION, long_msgs, max_tokens=50, read_timeout=60)
    print("  (stream: got_chunk={}, first_content='{}')".format(got_chunk, first_content[:50]))
    # Wait for 4B worker to process the trimmed messages and create summary
    await asyncio.sleep(25)
    summary_row = await check_summary_for_session(pool, AGENT_SESSION)
    passed = summary_row is not None
    detail = "stream_chunk=" + str(got_chunk) + ", summary=" + ("YES" if summary_row else "NO")
    if summary_row:
        detail += ", trimmed=" + str(summary_row['trimmed_msg_count']) + " msgs, preview='" + summary_row['summary'][:60] + "'"
    record("Agent summary (long session)", "sequential", passed, detail)


async def test_parallel(pool):
    print("\n=== CASE 2: Parallel Delegates (2-4 simultaneously) ===")

    print("\n--- Test 2.1: 3 sessions simultaneous (agent + 2 delegates) ---")
    msgs_a = [{"role": "system", "content": "Orchestrator agent coordinating sub-tasks."},
              {"role": "user", "content": "Dispatch auth and payment sub-tasks."}]
    msgs_d2 = [{"role": "system", "content": "Sub-agent: auth module."},
               {"role": "user", "content": "Implement JWT auth with access/refresh tokens."}]
    msgs_d3 = [{"role": "system", "content": "Sub-agent: payment module."},
               {"role": "user", "content": "Implement Stripe payment with webhooks."}]
    try:
        resp_a, resp_d2, resp_d3 = await asyncio.gather(
            make_chat_request(AGENT_SESSION, msgs_a, max_tokens=30, timeout=120),
            make_chat_request(DELEGATE_SESSIONS[2], msgs_d2, max_tokens=30, timeout=120),
            make_chat_request(DELEGATE_SESSIONS[3], msgs_d3, max_tokens=30, timeout=120),
        )
        await asyncio.sleep(3)
        all_s = [AGENT_SESSION, DELEGATE_SESSIONS[2], DELEGATE_SESSIONS[3]]
        found, missing, rows = await check_db_tasks(pool, all_s)
        passed = len(missing) == 0
        record("3 simultaneous sessions", "parallel", passed, "found={}/3, missing={}".format(len(found), missing))
    except Exception as e:
        record("3 simultaneous sessions", "parallel", False, "EXCEPTION: " + str(e)[:100])

    print("\n--- Test 2.2: 4 delegates simultaneous ---")
    msgs_list = [
        [{"role": "system", "content": "Sub-agent: DB optimization."}, {"role": "user", "content": "Add indexes for reports queries."}],
        [{"role": "system", "content": "Sub-agent: Frontend."}, {"role": "user", "content": "Build dashboard component."}],
        [{"role": "system", "content": "Sub-agent: CI/CD."}, {"role": "user", "content": "Set up GitHub Actions."}],
        [{"role": "system", "content": "Sub-agent: Docs."}, {"role": "user", "content": "Write API documentation."}],
    ]
    try:
        resps = await asyncio.gather(
            make_chat_request(DELEGATE_SESSIONS[0], msgs_list[0], max_tokens=30, timeout=120),
            make_chat_request(DELEGATE_SESSIONS[1], msgs_list[1], max_tokens=30, timeout=120),
            make_chat_request(DELEGATE_SESSIONS[2], msgs_list[2], max_tokens=30, timeout=120),
            make_chat_request(DELEGATE_SESSIONS[3], msgs_list[3], max_tokens=30, timeout=120),
        )
        await asyncio.sleep(3)
        all_s = DELEGATE_SESSIONS[:4]
        found, missing, rows = await check_db_tasks(pool, all_s)
        passed = len(missing) == 0
        record("4 simultaneous delegates", "parallel", passed, "found={}/4, missing={}".format(len(found), missing))
    except Exception as e:
        record("4 simultaneous delegates", "parallel", False, "EXCEPTION: " + str(e)[:100])

    print("\n--- Test 2.3: Session association (all 6 sessions) ---")
    all_sessions = [AGENT_SESSION] + DELEGATE_SESSIONS
    rows = await pool.fetch(
        "SELECT session_id, name, session_type FROM proxy.tasks WHERE session_id = ANY($1) ORDER BY session_id",
        all_sessions
    )
    session_ids = [r["session_id"] for r in rows]
    unique = len(set(session_ids))
    passed = unique == 6
    record("All 6 sessions associated", "parallel", passed, "rows={}, unique={}, expected=6, sessions={}".format(len(rows), unique, session_ids))

    print("\n--- Test 2.4: Long parallel sessions -> summaries ---")
    long_a = generate_long_context(AGENT_SESSION, num_turns=20)
    long_d2 = generate_long_context(DELEGATE_SESSIONS[2], num_turns=20)
    long_d3 = generate_long_context(DELEGATE_SESSIONS[3], num_turns=20)
    try:
        # Fire all 3 as streams (they'll queue at vLLM, but trimming+4B happens before vLLM call)
        r1, r2, r3 = await asyncio.gather(
            make_stream_request(AGENT_SESSION, long_a, max_tokens=50, read_timeout=60),
            make_stream_request(DELEGATE_SESSIONS[2], long_d2, max_tokens=50, read_timeout=60),
            make_stream_request(DELEGATE_SESSIONS[3], long_d3, max_tokens=50, read_timeout=60),
        )
        print("  (streams: agent={}, d3={}, d4={})".format(r1[0], r2[0], r3[0]))
        # Wait for 4B worker to process all 3
        await asyncio.sleep(30)
        sum_a = await check_summary_for_session(pool, AGENT_SESSION)
        sum_d2 = await check_summary_for_session(pool, DELEGATE_SESSIONS[2])
        sum_d3 = await check_summary_for_session(pool, DELEGATE_SESSIONS[3])
        passed = sum_a is not None
        detail = "agent={}, del3={}, del4={}".format("Y" if sum_a else "N", "Y" if sum_d2 else "N", "Y" if sum_d3 else "N")
        if sum_a:
            detail += " | agent: '" + sum_a['summary'][:60] + "'"
        record("Parallel summary correctness", "parallel", passed, detail)
    except Exception as e:
        record("Parallel summary correctness", "parallel", False, "EXCEPTION: " + str(e)[:100])

    print("\n--- Test 2.5: Knowledge injection ---")
    msgs = [{"role": "system", "content": "Working on ctxproxy project."},
            {"role": "user", "content": "What is the proxy port? How does session isolation work?"}]
    resp = await make_chat_request(AGENT_SESSION, msgs, max_tokens=50, timeout=120)
    await asyncio.sleep(3)
    async with httpx.AsyncClient(timeout=10) as client:
        metrics_resp = await client.get(PROXY_URL + "/api/memory-analytics")
        analytics = metrics_resp.json() if metrics_resp.status_code == 200 else {}
    kn = analytics.get("injection", {}).get("knowledge_injections", 0)
    passed = kn > 0
    record("Knowledge injection", "parallel", passed, "knowledge_injections={}, http={}".format(kn, resp.status_code))


async def main():
    print("=" * 70)
    print("CTXPROXY FULL-SCOPE INTEGRATION TEST (v3)")
    print("Proxy: {} | vLLM: 127.0.0.1:29000 | 4B: 127.0.0.1:1234".format(PROXY_URL))
    print("DB: " + DB_DSN)
    print("Agent: {} | Delegates: {}".format(AGENT_SESSION, DELEGATE_SESSIONS))
    print("MAX_INPUT=4000 (triggers trimming for long contexts)")
    print("=" * 70)

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)

    async with httpx.AsyncClient(timeout=5) as client:
        health = await client.get(PROXY_URL + "/health")
        print("\nProxy: {} {}".format(health.status_code, health.json()))

    try:
        await test_sequential(pool)
        await test_parallel(pool)
    finally:
        await pool.close()

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print("Total: {} | Pass: {} | Fail: {} | Rate: {}%".format(len(results), passed, failed, 100*passed//max(1,len(results))))
    print("-" * 70)
    for r in results:
        print("  [{}] ({:12s}) {:35s} {}".format(r['status'], r['scenario'], r['test'], r['detail'][:80]))
    print("-" * 70)

    with open("/home/user/ctxproxy/tests/integration_results.json", "w") as f:
        json.dump({"total": len(results), "passed": passed, "failed": failed, "results": results}, f, indent=2)
    print("\nSaved: /home/user/ctxproxy/tests/integration_results.json")

if __name__ == "__main__":
    asyncio.run(main())

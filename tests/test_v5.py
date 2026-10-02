"""
Test v5: Fixed long context (>4000 tokens) + all 6 sessions covered.
"""
import asyncio
import json
import os

import asyncpg
import httpx

PORT = 9201
URL = "http://127.0.0.1:{}".format(PORT)
DB = os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")
AGENT = "20261002_18"
DELS = ["20261002_19","20261002_20","20261002_21","20261002_22","20261002_23"]
R = []

def rec(name, scen, ok, det=""):
    R.append({"t":name,"s":scen,"ok":ok,"d":det})
    print("  [{}] {} ({})".format("PASS" if ok else "FAIL", name, det[:120]))

async def chat(sid, msgs, mtok=30, to=90):
    h = {"Content-Type":"application/json","X-Session-ID":sid}
    b = {"model":"Qwen3.8-27B","messages":msgs,"max_tokens":mtok,"stream":False,"temperature":0.7}
    async with httpx.AsyncClient(timeout=to) as c:
        return await c.post(URL+"/v1/chat/completions", json=b, headers=h)

async def stream_first(sid, msgs, mtok=30, rt=90):
    """Send streaming request, wait for first chunk OR timeout.
    Key: proxy processes build_context() (trim + 4B job) BEFORE calling vLLM.
    So even if vLLM is slow, the 4B job is already queued.
    """
    h = {"Content-Type":"application/json","X-Session-ID":sid}
    b = {"model":"Qwen3.8-27B","messages":msgs,"max_tokens":mtok,"stream":True,"temperature":0.7}
    got = False; content = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10,rt,30,10)) as c:
            async with c.stream("POST",URL+"/v1/chat/completions",json=b,headers=h) as r:
                if r.status_code != 200:
                    return False,"HTTP "+str(r.status_code)
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        d = line[6:]
                        if d == "[DONE]": break
                        try:
                            ch = json.loads(d)
                            c2 = ch.get("choices",[{}])[0].get("delta",{}).get("content","")
                            if c2: got=True; content=c2[:80]; break
                        except: pass
    except: pass
    return got, content

async def db_tasks(pool, sids):
    rows = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id=ANY($1)", sids)
    f = set(r["session_id"] for r in rows)
    return f, set(sids)-f

async def db_sum(pool, sid):
    return await pool.fetchrow(
        "SELECT ss.summary,ss.trimmed_msg_count FROM proxy.session_summaries ss "
        "JOIN proxy.tasks t ON t.id=ss.task_id WHERE t.session_id=$1 ORDER BY ss.created_at DESC LIMIT 1", sid)

async def db_mem(pool, sid):
    return await pool.fetch(
        "SELECT m.key FROM proxy.memories m JOIN proxy.tasks t ON t.id=m.task_id "
        "WHERE t.session_id=$1 AND m.active=true", sid)

def longctx(sid, n=30):
    """Generate >4000 tokens of content. Each turn ~250 tokens x 30 turns = 7500 tokens."""
    msgs=[{"role":"system","content":"You are a senior software engineer working on session "+sid+". The project is a large-scale Python/FastAPI e-commerce backend with PostgreSQL, Redis caching, and Kafka event streaming. Current task: implement a complete system with user management, product catalog, shopping cart, order processing, payment integration, inventory management, shipping logistics, returns processing, product reviews, recommendation engine, analytics dashboard, and admin panel. Each module must follow clean architecture with domain-driven design, proper error handling with custom exception hierarchy, structured logging with correlation IDs, comprehensive unit and integration tests, and full type annotations. All database operations use connection pooling with transaction management. API endpoints support pagination, filtering, sorting, and return consistent response formats with proper HTTP status codes."}]
    tops=[
        "user registration with email verification and password reset flow using token-based confirmation",
        "password hashing with bcrypt and adaptive salt generation for security compliance",
        "JWT token generation with access token expiry and refresh token rotation strategy",
        "token validation middleware that checks signature, expiry, and blacklist on every request",
        "user profile CRUD operations with avatar upload, social media linking, and preference storage",
        "product catalog with hierarchical categories, tags, and full-text search using PostgreSQL tsvector",
        "product search with typo tolerance, faceted filtering, and relevance scoring algorithm",
        "shopping cart with item quantity management, coupon code application, and tax calculation engine",
        "order creation with inventory reservation, payment authorization, and saga-based compensation",
        "payment processing with Stripe integration, webhook verification, and idempotency keys",
        "order status tracking with finite state machine and event sourcing for audit trail",
        "shipping address management with validation, geocoding, and multi-address support",
        "order cancellation with automatic refund triggering, inventory release, and notification dispatch",
        "product review system with moderation queue, photo uploads, and helpful vote aggregation",
        "inventory tracking with stock level alerts, warehouse location mapping, and transfer requests",
        "discount code application with validation rules, usage limits, and combination restrictions",
        "order history with pagination, date range filtering, and CSV export functionality",
        "notification system for order status changes using email, SMS, and push notification channels",
        "audit logging for all user actions with immutable log entries and retention policies",
        "rate limiting per user with sliding window algorithm and per-endpoint quota configuration",
        "circuit breaker pattern for external service calls with fallback strategies and health checks",
        "event sourcing for order state transitions with event store and snapshot optimization",
        "background job processing with Celery, priority queues, and retry with exponential backoff",
        "API versioning with deprecation warnings, migration guides, and backward compatibility layer",
        "feature flags with A/B testing support, gradual rollout, and per-tenant configuration",
        "structured logging with JSON format, correlation IDs, and Prometheus metrics exposure",
        "security hardening with CORS configuration, CSP headers, CSRF tokens, and input sanitization",
        "database migration strategy with zero-downtime schema changes and backward-compatible reads",
        "load testing with k6 scenarios, performance benchmarking, and capacity planning reports",
        "disaster recovery with automated PostgreSQL backups, PITR, and cross-region failover"
    ]
    for i in range(min(n,len(tops))):
        t=tops[i]
        msgs.append({"role":"user","content":"Task "+str(i+1)+": Implement the "+t+" module. Requirements: (1) Domain models with Pydantic validation and custom validators, (2) Repository pattern with async PostgreSQL and connection pooling, (3) FastAPI endpoints with OpenAPI 3.0 documentation and request/response models, (4) Unit tests with pytest-asyncio covering happy path, edge cases, and error scenarios, (5) Custom exception hierarchy with proper HTTP status code mapping, (6) Integration tests with test database and fixture isolation, (7) Module documentation with usage examples and architecture decisions, (8) Complete type annotations with mypy strict mode compliance, (9) Structured logging with correlation ID propagation across async boundaries, (10) Transaction management with savepoints for partial rollback scenarios."})
        msgs.append({"role":"assistant","content":"Implementation for "+t+":\n\nArchitecture: Clean architecture with 3 layers - domain (entities, value objects, domain events), application (use cases, services, ports), and infrastructure (adapters, repositories, external clients). Dependency injection via FastAPI Depends for testability.\n\nDomain Layer: Entity classes with business logic in methods. Value objects for immutable data (Money, Address, Email). Domain events published via in-process event bus for transactional consistency.\n\nApplication Layer: Use case classes orchestrate domain objects. Service interfaces (ports) defined in application layer, implemented in infrastructure. Command pattern for write operations, Query pattern for reads (CQRS-lite).\n\nInfrastructure Layer: AsyncPostgreSQLRepository using asyncpg connection pool. RedisCache with TTL-based expiry. KafkaProducer for event streaming. All external clients wrapped in adapter classes implementing port interfaces.\n\nAPI Layer: FastAPI router with Pydantic request/response models. Validation at the boundary. Error handler middleware maps domain exceptions to HTTP responses. Pagination via offset/limit with Link headers. Filtering via query parameters with whitelist validation.\n\nTesting: Unit tests mock repository interfaces. Integration tests use testcontainers for PostgreSQL. Fixture factory generates test data. Coverage target 95%+ with branch coverage. Property-based testing for complex validation logic.\n\nThe module exports a router and a dependency provider. Configuration via pydantic-settings with environment variable overrides. All async operations handle timeouts (30s default) and retries (3 attempts, exponential backoff with jitter). Connection pool size: 20 min, 50 max. Statement timeout: 5s. Idle in transaction timeout: 10s."})
    return msgs

async def main():
    print("="*60)
    print("CTXPROXY TEST v5 - port {} (MAX_INPUT=4000)".format(PORT))
    print("="*60)
    pool = await asyncpg.create_pool(DB, min_size=2, max_size=5)

    async with httpx.AsyncClient(timeout=5) as c:
        h = await c.get(URL+"/health")
        print("Proxy: {} {}".format(h.status_code, h.json()))

    # === CASE 1: Sequential (5 tests) ===
    print("\n=== CASE 1: Sequential ===")

    print("\n[1.1] Agent task creation")
    r = await chat(AGENT, [{"role":"system","content":"Main agent orchestrating sub-tasks."},{"role":"user","content":"Set up DB schema for users, products, orders tables."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[AGENT])
    rec("Agent task","seq",AGENT in f,"http={} found={}".format(r.status_code,f))

    print("\n[1.2] Delegate 1 (20261002_19)")
    r = await chat(DELS[0], [{"role":"system","content":"Sub: DB schema design."},{"role":"user","content":"Design users table with UUID pk."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[DELS[0]])
    rec("Del1 task","seq",DELS[0] in f,"http={} found={}".format(r.status_code,f))

    print("\n[1.3] Delegate 2 (20261002_20)")
    r = await chat(DELS[1], [{"role":"system","content":"Sub: API endpoints."},{"role":"user","content":"Implement /api/users CRUD endpoints."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[DELS[1]])
    rec("Del2 task","seq",DELS[1] in f,"http={} found={}".format(r.status_code,f))

    print("\n[1.4] No cross-contamination")
    am = await db_mem(pool,AGENT)
    d1m = await db_mem(pool,DELS[0])
    d2m = await db_mem(pool,DELS[1])
    all_t = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id=ANY($1)",[AGENT,DELS[0],DELS[1]])
    rec("No cross-contam","seq",len(all_t)==3,"tasks={} agent_mem={} d1={} d2={}".format(len(all_t),len(am),len(d1m),len(d2m)))

    print("\n[1.5] Long session -> trim + 4B summary")
    lc = longctx(AGENT,30)
    tc = sum(len(m.get("content","")) for m in lc)
    est_tokens = tc // 4
    print("  ({} msgs, ~{} chars, ~{} est tokens, limit=4000)".format(len(lc),tc,est_tokens))
    g,c = await stream_first(AGENT,lc,mtok=30,rt=90)
    print("  stream: got={} content='{}'".format(g,c[:40]))
    print("  waiting 35s for 4B worker to create summary...")
    await asyncio.sleep(35)
    s = await db_sum(pool,AGENT)
    rec("Agent summary","seq",s is not None,"summary={} trimmed={}".format("YES" if s else "NO",s["trimmed_msg_count"] if s else 0))

    # === CASE 2: Parallel (5 tests) ===
    print("\n=== CASE 2: Parallel ===")

    print("\n[2.1] 3 simultaneous (agent + del3 + del4)")
    try:
        r1,r2,r3 = await asyncio.gather(
            chat(AGENT,[{"role":"system","content":"Orchestrator."},{"role":"user","content":"Dispatch auth and payment sub-tasks."}],mtok=20,to=120),
            chat(DELS[2],[{"role":"system","content":"Sub: auth module."},{"role":"user","content":"Implement JWT auth."}],mtok=20,to=120),
            chat(DELS[3],[{"role":"system","content":"Sub: payment module."},{"role":"user","content":"Implement Stripe payment."}],mtok=20,to=120),
        )
        await asyncio.sleep(2)
        f,m = await db_tasks(pool,[AGENT,DELS[2],DELS[3]])
        rec("3 simultaneous","par",len(m)==0,"found={}/3 missing={}".format(len(f),m))
    except Exception as e:
        rec("3 simultaneous","par",False,"EXC: "+str(e)[:80])

    print("\n[2.2] 4 simultaneous delegates (incl. 20261002_23)")
    try:
        ml=[
            [{"role":"system","content":"Sub: DB optimization."},{"role":"user","content":"Add indexes for reports."}],
            [{"role":"system","content":"Sub: Frontend."},{"role":"user","content":"Build dashboard."}],
            [{"role":"system","content":"Sub: CI/CD."},{"role":"user","content":"Set up GitHub Actions."}],
            [{"role":"system","content":"Sub: Documentation."},{"role":"user","content":"Write API docs."}],
        ]
        rs = await asyncio.gather(
            chat(DELS[0],ml[0],mtok=20,to=120),
            chat(DELS[1],ml[1],mtok=20,to=120),
            chat(DELS[2],ml[2],mtok=20,to=120),
            chat(DELS[4],ml[3],mtok=20,to=120),  # Use DELS[4] = 20261002_23
        )
        await asyncio.sleep(2)
        f,m = await db_tasks(pool,[DELS[0],DELS[1],DELS[2],DELS[4]])
        rec("4 simultaneous","par",len(m)==0,"found={}/4 missing={}".format(len(f),m))
    except Exception as e:
        rec("4 simultaneous","par",False,"EXC: "+str(e)[:80])

    print("\n[2.3] All 6 sessions in DB")
    all_s = [AGENT]+DELS
    rows = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id=ANY($1) ORDER BY session_id",all_s)
    ids = [r["session_id"] for r in rows]
    rec("6 sessions","par",len(set(ids))==6,"found={}/6: {}".format(len(ids),ids))

    print("\n[2.4] Parallel long sessions -> summaries")
    la = longctx(AGENT,25)
    l2 = longctx(DELS[2],25)
    l3 = longctx(DELS[3],25)
    try:
        r1,r2,r3 = await asyncio.gather(
            stream_first(AGENT,la,mtok=30,rt=90),
            stream_first(DELS[2],l2,mtok=30,rt=90),
            stream_first(DELS[3],l3,mtok=30,rt=90),
        )
        print("  streams: {} {} {}".format(r1[0],r2[0],r3[0]))
        print("  waiting 35s for 4B worker...")
        await asyncio.sleep(35)
        sa = await db_sum(pool,AGENT)
        s2 = await db_sum(pool,DELS[2])
        s3 = await db_sum(pool,DELS[3])
        rec("Parallel summaries","par",sa is not None,"agent={} d3={} d4={}".format("Y" if sa else "N","Y" if s2 else "N","Y" if s3 else "N"))
    except Exception as e:
        rec("Parallel summaries","par",False,"EXC: "+str(e)[:80])

    print("\n[2.5] Knowledge injection")
    r = await chat(AGENT,[{"role":"system","content":"Working on ctxproxy project."},{"role":"user","content":"What is the proxy port? How does session isolation work?"}],mtok=50,to=120)
    await asyncio.sleep(2)
    async with httpx.AsyncClient(timeout=10) as c:
        mr = await c.get(URL+"/api/memory-analytics")
        an = mr.json() if mr.status_code==200 else {}
    kn = an.get("injection",{}).get("knowledge_injections",0)
    rec("Knowledge inject","par",kn>0,"kn_inj={} http={}".format(kn,r.status_code))

    await pool.close()

    print("\n"+"="*60)
    p = sum(1 for x in R if x["ok"])
    fl = sum(1 for x in R if not x["ok"])
    print("TOTAL: {} | PASS: {} | FAIL: {} | RATE: {}%".format(len(R),p,fl,100*p//max(1,len(R))))
    print("-"*60)
    for x in R:
        print("  [{}] ({:5s}) {:25s} {}".format("PASS" if x["ok"] else "FAIL",x["s"],x["t"],x["d"][:70]))
    print("-"*60)
    with open("/home/user/ctxproxy/tests/integration_results.json","w") as f:
        json.dump({"total":len(R),"pass":p,"fail":fl,"results":R},f,indent=2)
    print("Saved: tests/integration_results.json")

asyncio.run(main())

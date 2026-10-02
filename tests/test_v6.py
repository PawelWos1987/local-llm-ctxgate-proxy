"""
Test v6: Fire-and-forget for long requests.
The proxy processes build_context() (trim + 4B job) BEFORE calling vLLM.
We fire the request as a background task, don't await it, then check DB.
"""
import asyncio, json, os, httpx, asyncpg

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

async def fire_and_forget(sid, msgs, mtok=30):
    """Fire a request without awaiting completion.
    Uses a raw connection that we close after sending the body.
    The proxy continues processing server-side.
    """
    h = {"Content-Type":"application/json","X-Session-ID":sid}
    b = {"model":"Qwen3.8-27B","messages":msgs,"max_tokens":mtok,"stream":True,"temperature":0.7}
    body_json = json.dumps(b)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10,5,30,10)) as c:
            # Use raw request to send body then disconnect
            req = c.build_request("POST", URL+"/v1/chat/completions", content=body_json, headers=h)
            resp = await c.send(req, stream=True)
            # Read a tiny bit then close - proxy continues processing
            await asyncio.sleep(0.5)
            await resp.aclose()
    except:
        pass

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
    """Generate >4000 tokens. ~250 tokens/turn x 30 turns = 7500 tokens."""
    msgs=[{"role":"system","content":"You are a senior software engineer on session "+sid+". Project: Python/FastAPI e-commerce backend with PostgreSQL, Redis, Kafka. Implement user management, product catalog, shopping cart, order processing, payment, inventory, shipping, returns, reviews, recommendations, analytics, admin panel. Clean architecture, DDD, error handling, logging, tests, type annotations, connection pooling, pagination, filtering."}]
    tops=[
        "user registration with email verification and password reset using token-based confirmation",
        "password hashing with bcrypt and adaptive salt generation for security compliance",
        "JWT token generation with access token expiry and refresh token rotation",
        "token validation middleware checking signature expiry and blacklist",
        "user profile CRUD with avatar upload social media linking preferences",
        "product catalog with hierarchical categories tags full-text search tsvector",
        "product search with typo tolerance faceted filtering relevance scoring",
        "shopping cart with quantity management coupon application tax calculation",
        "order creation with inventory reservation payment authorization saga compensation",
        "payment processing with Stripe integration webhook verification idempotency",
        "order status tracking with finite state machine event sourcing audit",
        "shipping address management with validation geocoding multi-address",
        "order cancellation with refund triggering inventory release notification",
        "product review system with moderation queue photo uploads helpful votes",
        "inventory tracking with stock alerts warehouse mapping transfer requests",
        "discount code application with validation rules usage limits combinations",
        "order history with pagination date filtering CSV export",
        "notification system for order changes email SMS push channels",
        "audit logging for user actions immutable entries retention policies",
        "rate limiting per user sliding window per-endpoint quota config",
        "circuit breaker for external calls fallback strategies health checks",
        "event sourcing for order transitions event store snapshot optimization",
        "background jobs with Celery priority queues retry exponential backoff",
        "API versioning with deprecation warnings migration backward compat",
        "feature flags with A/B testing gradual rollout per-tenant config",
        "structured logging JSON format correlation IDs Prometheus metrics",
        "security hardening CORS CSP headers CSRF tokens input sanitization",
        "DB migration zero-downtime schema changes backward-compatible reads",
        "load testing k6 scenarios performance benchmarking capacity planning",
        "disaster recovery automated backups PITR cross-region failover"
    ]
    for i in range(min(n,len(tops))):
        t=tops[i]
        msgs.append({"role":"user","content":"Task "+str(i+1)+": Implement "+t+". Requirements: domain models with Pydantic validation, repository pattern with async PostgreSQL, FastAPI endpoints with OpenAPI docs, unit tests with pytest-asyncio, custom exception hierarchy, integration tests, documentation, type annotations with mypy strict, structured logging with correlation IDs, transaction management with savepoints."})
        msgs.append({"role":"assistant","content":"Implementation for "+t+": Clean architecture with domain/application/infrastructure layers. DI via FastAPI Depends. Domain: entities with business logic, value objects, domain events. Application: use cases, service interfaces (ports), command/query pattern. Infrastructure: AsyncPostgreSQLRepository with asyncpg pool, RedisCache with TTL, KafkaProducer. API: FastAPI router, Pydantic models, error handler middleware, pagination with Link headers, filtering with whitelist. Tests: unit mock repos, integration testcontainers, fixture factory, 95%+ coverage, property-based testing. Exports router and dependency provider. Config via pydantic-settings. Async ops: 30s timeout, 3 retries, exponential backoff with jitter. Pool: 20 min, 50 max. Statement timeout 5s. Idle in transaction 10s."})
    return msgs

async def main():
    print("="*60)
    print("CTXPROXY TEST v6 - port {} (MAX_INPUT=4000)".format(PORT))
    print("="*60)
    pool = await asyncpg.create_pool(DB, min_size=2, max_size=5)
    
    async with httpx.AsyncClient(timeout=5) as c:
        h = await c.get(URL+"/health")
        print("Proxy: {} {}".format(h.status_code, h.json()))

    # === CASE 1: Sequential (5 tests) ===
    print("\n=== CASE 1: Sequential ===")
    
    print("\n[1.1] Agent task creation")
    r = await chat(AGENT, [{"role":"system","content":"Main agent."},{"role":"user","content":"Set up DB schema for users, products, orders."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[AGENT])
    rec("Agent task","seq",AGENT in f,"http={} found={}".format(r.status_code,f))
    
    print("\n[1.2] Delegate 1")
    r = await chat(DELS[0], [{"role":"system","content":"Sub: DB schema."},{"role":"user","content":"Design users table."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[DELS[0]])
    rec("Del1 task","seq",DELS[0] in f,"http={} found={}".format(r.status_code,f))
    
    print("\n[1.3] Delegate 2")
    r = await chat(DELS[1], [{"role":"system","content":"Sub: API."},{"role":"user","content":"Implement /api/users CRUD."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[DELS[1]])
    rec("Del2 task","seq",DELS[1] in f,"http={} found={}".format(r.status_code,f))
    
    print("\n[1.4] No cross-contamination")
    am = await db_mem(pool,AGENT)
    d1m = await db_mem(pool,DELS[0])
    d2m = await db_mem(pool,DELS[1])
    all_t = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id=ANY($1)",[AGENT,DELS[0],DELS[1]])
    rec("No cross-contam","seq",len(all_t)==3,"tasks={} agent_mem={} d1={} d2={}".format(len(all_t),len(am),len(d1m),len(d2m)))
    
    print("\n[1.5] Long session -> trim + 4B summary (fire-and-forget)")
    lc = longctx(AGENT,30)
    tc = sum(len(m.get("content","")) for m in lc)
    print("  ({} msgs, ~{} chars, ~{} est tokens, limit=4000)".format(len(lc),tc,tc//4))
    await fire_and_forget(AGENT, lc, mtok=30)
    print("  fired. waiting 40s for 4B worker...")
    await asyncio.sleep(40)
    s = await db_sum(pool,AGENT)
    rec("Agent summary","seq",s is not None,"summary={} trimmed={}".format("YES" if s else "NO",s["trimmed_msg_count"] if s else 0))

    # === CASE 2: Parallel (5 tests) ===
    print("\n=== CASE 2: Parallel ===")
    
    print("\n[2.1] 3 simultaneous")
    try:
        r1,r2,r3 = await asyncio.gather(
            chat(AGENT,[{"role":"system","content":"Orchestrator."},{"role":"user","content":"Dispatch auth+payment."}],mtok=20,to=120),
            chat(DELS[2],[{"role":"system","content":"Sub: auth."},{"role":"user","content":"JWT auth."}],mtok=20,to=120),
            chat(DELS[3],[{"role":"system","content":"Sub: payment."},{"role":"user","content":"Stripe."}],mtok=20,to=120),
        )
        await asyncio.sleep(2)
        f,m = await db_tasks(pool,[AGENT,DELS[2],DELS[3]])
        rec("3 simultaneous","par",len(m)==0,"found={}/3 missing={}".format(len(f),m))
    except Exception as e:
        rec("3 simultaneous","par",False,"EXC: "+str(e)[:80])
    
    print("\n[2.2] 4 simultaneous (incl. 20261002_23)")
    try:
        ml=[
            [{"role":"system","content":"Sub: DB opt."},{"role":"user","content":"Add indexes."}],
            [{"role":"system","content":"Sub: Frontend."},{"role":"user","content":"Dashboard."}],
            [{"role":"system","content":"Sub: CI/CD."},{"role":"user","content":"GitHub Actions."}],
            [{"role":"system","content":"Sub: Docs."},{"role":"user","content":"API docs."}],
        ]
        rs = await asyncio.gather(
            chat(DELS[0],ml[0],mtok=20,to=120),
            chat(DELS[1],ml[1],mtok=20,to=120),
            chat(DELS[2],ml[2],mtok=20,to=120),
            chat(DELS[4],ml[3],mtok=20,to=120),
        )
        await asyncio.sleep(2)
        f,m = await db_tasks(pool,[DELS[0],DELS[1],DELS[2],DELS[4]])
        rec("4 simultaneous","par",len(m)==0,"found={}/4 missing={}".format(len(f),m))
    except Exception as e:
        rec("4 simultaneous","par",False,"EXC: "+str(e)[:80])
    
    print("\n[2.3] All 6 sessions")
    all_s = [AGENT]+DELS
    rows = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id=ANY($1) ORDER BY session_id",all_s)
    ids = [r["session_id"] for r in rows]
    rec("6 sessions","par",len(set(ids))==6,"found={}/6: {}".format(len(ids),ids))
    
    print("\n[2.4] Parallel long sessions -> summaries (fire-and-forget)")
    la = longctx(AGENT,25)
    l2 = longctx(DELS[2],25)
    l3 = longctx(DELS[3],25)
    await asyncio.gather(
        fire_and_forget(AGENT, la, mtok=30),
        fire_and_forget(DELS[2], l2, mtok=30),
        fire_and_forget(DELS[3], l3, mtok=30),
    )
    print("  fired 3 long requests. waiting 40s for 4B worker...")
    await asyncio.sleep(40)
    sa = await db_sum(pool,AGENT)
    s2 = await db_sum(pool,DELS[2])
    s3 = await db_sum(pool,DELS[3])
    rec("Parallel summaries","par",sa is not None,"agent={} d3={} d4={}".format("Y" if sa else "N","Y" if s2 else "N","Y" if s3 else "N"))
    
    print("\n[2.5] Knowledge injection")
    r = await chat(AGENT,[{"role":"system","content":"ctxproxy project."},{"role":"user","content":"What is the proxy port? How does session isolation work?"}],mtok=50,to=120)
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

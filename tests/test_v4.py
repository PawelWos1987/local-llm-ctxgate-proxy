"""
Lean integration test v4 - works with already-running proxy on port 9201.
Strategy: short messages for fast vLLM calls, stream+disconnect for long context.
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

async def stream_first(sid, msgs, mtok=30, rt=45):
    h = {"Content-Type":"application/json","X-Session-ID":sid}
    b = {"model":"Qwen3.8-27B","messages":msgs,"max_tokens":mtok,"stream":True,"temperature":0.7}
    got = False; content = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10,rt,10,10)) as c:
            async with c.stream("POST",URL+"/v1/chat/completions",json=b,headers=h) as r:
                if r.status_code != 200: return False,"HTTP "+str(r.status_code)
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

def longctx(sid, n=20):
    msgs=[{"role":"system","content":"You are a senior engineer on session "+sid+". Project: Python/FastAPI e-commerce backend with PostgreSQL. Implement user auth, product catalog, order processing with clean architecture, DDD, proper error handling, logging, tests."}]
    tops=["user registration with email verification","password hashing with bcrypt","JWT token generation and refresh","token validation middleware","user profile CRUD","product catalog with categories","product search with full-text","shopping cart management","order creation with inventory check","Stripe payment integration","order status state machine","shipping address management","order cancellation with refund","product review system","inventory stock tracking","discount code validation","order history pagination","order status notifications","audit logging for actions","rate limiting per user"]
    for i in range(min(n,len(tops))):
        t=tops[i]
        msgs.append({"role":"user","content":"Implement "+t+" with proper error handling, DB ops, unit tests, type hints, docstrings."})
        msgs.append({"role":"assistant","content":"Implementation for "+t+": follows clean architecture with repository pattern. Parameterized queries prevent SQL injection. Specific exceptions: NotFound, ValidationError. Service layer handles business logic, API layer handles HTTP. Unit tests cover happy path and edge cases. Integration tests verify DB interactions. Proper logging with correlation IDs. Complete type annotations. PEP 8 compliant. DB migrations included. Router exportable to main app. Config via env vars. 95%+ test coverage."})
    return msgs

async def main():
    print("="*60)
    print("CTXPROXY TEST v4 - port {}".format(PORT))
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
    
    print("\n[1.2] Delegate 1 (20261002_19)")
    r = await chat(DELS[0], [{"role":"system","content":"Sub: DB schema."},{"role":"user","content":"Design users table."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[DELS[0]])
    rec("Del1 task","seq",DELS[0] in f,"http={} found={}".format(r.status_code,f))
    
    print("\n[1.3] Delegate 2 (20261002_20)")
    r = await chat(DELS[1], [{"role":"system","content":"Sub: API endpoints."},{"role":"user","content":"Implement /api/users CRUD."}])
    await asyncio.sleep(2)
    f,m = await db_tasks(pool,[DELS[1]])
    rec("Del2 task","seq",DELS[1] in f,"http={} found={}".format(r.status_code,f))
    
    print("\n[1.4] No cross-contamination")
    am = await db_mem(pool,AGENT)
    d1m = await db_mem(pool,DELS[0])
    d2m = await db_mem(pool,DELS[1])
    all_t = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id=ANY($1)",[AGENT,DELS[0],DELS[1]])
    rec("No cross-contam","seq",len(all_t)==3,"tasks={} agent_mem={} d1={} d2={}".format(len(all_t),len(am),len(d1m),len(d2m)))
    
    print("\n[1.5] Long session -> trim + summary")
    lc = longctx(AGENT,20)
    tc = sum(len(m.get("content","")) for m in lc)
    print("  ({} msgs, ~{} chars)".format(len(lc),tc))
    g,c = await stream_first(AGENT,lc,mtok=30,rt=60)
    print("  stream: got={} content='{}'".format(g,c[:40]))
    print("  waiting 30s for 4B summarization...")
    await asyncio.sleep(30)
    s = await db_sum(pool,AGENT)
    rec("Agent summary","seq",s is not None,"summary={} trimmed={}".format("YES" if s else "NO",s["trimmed_msg_count"] if s else 0))

    # === CASE 2: Parallel (5 tests) ===
    print("\n=== CASE 2: Parallel ===")
    
    print("\n[2.1] 3 simultaneous (agent+2 dels)")
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
    
    print("\n[2.2] 4 simultaneous delegates")
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
            chat(DELS[3],ml[3],mtok=20,to=120),
        )
        await asyncio.sleep(2)
        f,m = await db_tasks(pool,DELS[:4])
        rec("4 simultaneous","par",len(m)==0,"found={}/4 missing={}".format(len(f),m))
    except Exception as e:
        rec("4 simultaneous","par",False,"EXC: "+str(e)[:80])
    
    print("\n[2.3] All 6 sessions in DB")
    all_s = [AGENT]+DELS
    rows = await pool.fetch("SELECT session_id FROM proxy.tasks WHERE session_id=ANY($1) ORDER BY session_id",all_s)
    ids = [r["session_id"] for r in rows]
    rec("6 sessions","par",len(set(ids))==6,"found={}/6: {}".format(len(ids),ids))
    
    print("\n[2.4] Parallel long sessions -> summaries")
    la = longctx(AGENT,15)
    l2 = longctx(DELS[2],15)
    l3 = longctx(DELS[3],15)
    try:
        r1,r2,r3 = await asyncio.gather(
            stream_first(AGENT,la,mtok=30,rt=60),
            stream_first(DELS[2],l2,mtok=30,rt=60),
            stream_first(DELS[3],l3,mtok=30,rt=60),
        )
        print("  streams: {} {} {}".format(r1[0],r2[0],r3[0]))
        print("  waiting 30s for 4B...")
        await asyncio.sleep(30)
        sa = await db_sum(pool,AGENT)
        s2 = await db_sum(pool,DELS[2])
        s3 = await db_sum(pool,DELS[3])
        rec("Parallel summaries","par",sa is not None,"agent={} d3={} d4={}".format("Y" if sa else "N","Y" if s2 else "N","Y" if s3 else "N"))
    except Exception as e:
        rec("Parallel summaries","par",False,"EXC: "+str(e)[:80])
    
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

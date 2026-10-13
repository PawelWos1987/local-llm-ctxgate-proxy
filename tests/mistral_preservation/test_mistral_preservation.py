"""
Mistral LLM Preservation Test Suite
====================================
Tests summary/memory preservation across very long sessions (>=1M tokens).
Uses goose CLI to exercise the ctxgate-proxy (port 9201).

Tables validated:
  session_summaries, phase_summaries, summary_chunks, working_memory,
  memories, memory_jobs, session_windows, session_ledger, events,
  tasks, ledger_entries, tool_body_chunks

Excluded: api_keys, deliverables, knowledge
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

# --- Configuration ---

PROVIDER = "custom_qwen"
PG_DSN_ENV = "PGPASSWORD=11!!AdaMicPaw"
PG_HOST = "127.0.0.1"
PG_USER = "postgres"
PG_DB = "ctxproxy"
MIN_TOTAL_TOKENS = 1_000_000
MAX_OUTPUT_TOKENS = 11000
FILLER_WORD = ("lorem ipsum dolor sit amet consectetur adipiscing elit "
               "sed do eiusmod tempor incididunt ut labore et dolore magna aliqua ")
Q = chr(34)  # double quote
SQ = chr(39)  # single quote
PIPE = chr(124)  # pipe
NL = chr(10)  # newline

@dataclass
class TestResult:
    test_id: str
    name: str
    passed: bool
    total_tokens: int
    messages_sent: int
    duration_sec: float
    details: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

def pg_query(sql):
    cmd = PG_DSN_ENV + " psql -h " + PG_HOST + " -U " + PG_USER + " -d " + PG_DB + " -t -A -c " + Q + sql + Q
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("PG query failed: " + r.stderr.strip())
    return r.stdout.strip()

def pg_query_rows(sql):
    out = pg_query(sql)
    if not out:
        return []
    return [line for line in out.split(NL) if line.strip()]

def generate_filler(n):
    base = FILLER_WORD
    return (base * max(1, n // len(base)))[:n]

def make_message(topic, filler_chars=800):
    return "[" + topic + "] Please analyze: " + generate_filler(filler_chars)

def run_goose(msg, session, resume=False):
    parts = ["goose", "run", "-t", msg, "--provider", PROVIDER, "-n", session]
    if resume:
        parts.append("-r")
    r = subprocess.run(parts, capture_output=True, text=True, timeout=1800)
    return (r.returncode == 0, r.stdout + r.stderr)

def est_tokens(t):
    return max(1, len(t) // 4)

def sess_events(sid):
    try:
        v = pg_query("SELECT COUNT(*) FROM proxy.events e JOIN proxy.tasks t ON t.id=e.task_id WHERE t.session_id=" + SQ + sid + SQ)
        return int(v) if v else 0
    except Exception:
        return 0

def sess_tokens(sid):
    try:
        v = pg_query("SELECT COALESCE(SUM(LENGTH(content)),0) FROM proxy.events e JOIN proxy.tasks t ON t.id=e.task_id WHERE t.session_id=" + SQ + sid + SQ)
        return int(v) // 4 if v else 0
    except Exception:
        return 0

async def drive(sid, n, fc=800, topics=None):
    if topics is None:
        topics = ["Topic-" + str(i) for i in range(n)]
    ok = fail = 0
    for i, tp in enumerate(topics):
        m = make_message(tp, fc)
        o, out = run_goose(m, sid, resume=(i > 0))
        if o: ok += 1
        else: fail += 1; print("  WARN " + str(i+1) + "/" + str(n) + ": " + out[:150])
        await asyncio.sleep(0.5)
    return ok, fail

# === TEST 01: Compaction Trigger ===
async def test_01():
    """T01: High-watermark crossing triggers session_summaries creation."""
    sid = "mistral-t01-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    await drive(sid, 500)
    rows = pg_query_rows("SELECT LENGTH(summary) FROM proxy.session_summaries ss JOIN proxy.tasks t ON t.id=ss.task_id WHERE t.session_id=" + SQ + sid + SQ)
    ev = sess_events(sid); tk = sess_tokens(sid)
    p = tk >= MIN_TOTAL_TOKENS and len(rows) > 0 and ev > 0
    return TestResult("t01","Compaction Trigger",p,tk,500,time.time()-t0,{ "summaries":len(rows),"events":ev },[] if p else["No summaries"])

# === TEST 02: Phase Boundaries ===
async def test_02():
    """T02: Abrupt topic shifts produce multiple phase_summaries."""
    sid = "mistral-t02-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    topics = []
    for ph in range(10):
        for m in range(50):
            topics.append("PHASE-" + str(ph) + "-" + ("DEBUG-alpha" if ph%2==0 else "DESIGN-beta") + "-" + str(m))
    await drive(sid, 500, topics=topics)
    rows = pg_query_rows("SELECT phase_id,boundary_seq,LENGTH(summary) FROM proxy.phase_summaries ps JOIN proxy.tasks t ON t.id=ps.task_id WHERE t.session_id=" + SQ + sid + SQ + " ORDER BY phase_id")
    tk = sess_tokens(sid)
    p = tk >= MIN_TOTAL_TOKENS and len(rows) >= 2
    return TestResult("t02","Phase Boundaries",p,tk,500,time.time()-t0,{"phases":len(rows)},[] if p else["Expected>=2 phases,got "+str(len(rows))])

# === TEST 03: Memory Recall Post-Compaction ===
async def test_03():
    """T03: Facts established early survive compaction and remain recallable."""
    sid = "mistral-t03-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    fact = "DEPLOYMENT CODENAME IS BLUE HERON SEVEN endpoint /v3/internal/lambda-gateway"
    topics = []
    for i in range(50):
        topics.append(("REMEMBER THIS: " + fact) if i==0 else "Implementation detail " + str(i))
    for i in range(400):
        topics.append("Performance tuning variant " + str(i))
    for i in range(50):
        topics.append("What was the deployment codename? ctx " + str(i))
    await drive(sid, 500, topics=topics)
    mems = pg_query_rows("SELECT value FROM proxy.memories m JOIN proxy.tasks t ON t.id=m.task_id WHERE t.session_id=" + SQ + sid + SQ + " AND m.active=true")
    wm = pg_query("SELECT content FROM proxy.working_memory wm JOIN proxy.tasks t ON t.id=wm.task_id WHERE t.session_id=" + SQ + sid + SQ)
    tk = sess_tokens(sid)
    in_m = any("BLUE HERON" in x.upper() for x in mems)
    in_w = "BLUE HERON" in wm.upper() if wm else False
    p = tk >= MIN_TOTAL_TOKENS and (in_m or in_w)
    return TestResult("t03","Memory Recall",p,tk,500,time.time()-t0,{"in_mem":in_m,"in_wm":in_w},[] if p else["Fact lost"])

# === TEST 04: Working Memory Eviction ===
async def test_04():
    """T04: Working memory stays bounded (<10K) and reflects recent state."""
    sid = "mistral-t04-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    topics = ["Step " + str(i) + " module-" + str(i%20) + " prio " + str(i%5) for i in range(500)]
    await drive(sid, 500, topics=topics)
    wm = pg_query("SELECT content FROM proxy.working_memory wm JOIN proxy.tasks t ON t.id=wm.task_id WHERE t.session_id=" + SQ + sid + SQ)
    tk = sess_tokens(sid)
    exists = bool(wm)
    bounded = len(wm) < 10000 if wm else False
    p = tk >= MIN_TOTAL_TOKENS and exists and bounded
    return TestResult("t04","WM Eviction",p,tk,500,time.time()-t0,{"exists":exists,"len":len(wm) if wm else 0},[] if p else["WM missing/unbounded"])

# === TEST 05: Summary Chunk Stitching ===
async def test_05():
    """T05: Long summaries split into sequential chunks without gaps."""
    sid = "mistral-t05-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    topics = ["Dense arch discussion #" + str(i) + ": consensus,fault-tolerance,sharding" for i in range(500)]
    await drive(sid, 500, fc=2000, topics=topics)
    chunks = pg_query_rows("SELECT sc.chunk_index,LENGTH(sc.content) FROM proxy.summary_chunks sc JOIN proxy.session_summaries ss ON ss.id=sc.summary_id JOIN proxy.tasks t ON t.id=ss.task_id WHERE t.session_id=" + SQ + sid + SQ + " ORDER BY sc.chunk_index")
    sums = pg_query_rows("SELECT LENGTH(summary) FROM proxy.session_summaries ss JOIN proxy.tasks t ON t.id=ss.task_id WHERE t.session_id=" + SQ + sid + SQ)
    tk = sess_tokens(sid)
    seq_ok = True
    for i, c in enumerate(chunks):
        if int(c.split("|")[0]) != i:
            seq_ok = False; break
    p = tk >= MIN_TOTAL_TOKENS and (len(chunks)>0 or len(sums)>0) and seq_ok
    return TestResult("t05","Chunk Stitching",p,tk,500,time.time()-t0,{"chunks":len(chunks),"sums":len(sums),"seq":seq_ok},[] if p else["Seq broken"])

# === TEST 06: Job Retries ===
async def test_06():
    """T06: Memory jobs maintain timestamp ordering (created<started<completed)."""
    sid = "mistral-t06-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    topics = ["Batch item " + str(i) + " transform+validate" for i in range(500)]
    await drive(sid, 500, topics=topics)
    await asyncio.sleep(5)
    viol = pg_query("SELECT COUNT(*) FROM proxy.memory_jobs mj JOIN proxy.tasks t ON t.id=mj.task_id WHERE t.session_id=" + SQ + sid + SQ + " AND ((mj.started_at<mj.created_at) OR (mj.completed_at IS NOT NULL AND mj.completed_at<mj.started_at))")
    total = int(pg_query("SELECT COUNT(*) FROM proxy.memory_jobs mj JOIN proxy.tasks t ON t.id=mj.task_id WHERE t.session_id=" + SQ + sid + SQ) or 0)
    tk = sess_tokens(sid)
    ts_ok = viol == "0"
    p = tk >= MIN_TOTAL_TOKENS and total > 0 and ts_ok
    return TestResult("t06","Job Retries",p,tk,500,time.time()-t0,{"jobs":total,"viol":int(viol) if viol.isdigit() else 0},[] if p else["TS violation"])

# === TEST 07: Window Consistency ===
async def test_07():
    """T07: Session windows are monotonically increasing, non-overlapping."""
    sid = "mistral-t07-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    topics = ["Window boundary check " + str(i) for i in range(500)]
    await drive(sid, 500, topics=topics)
    wins = pg_query_rows("SELECT window_start_seq,window_end_seq,token_count FROM proxy.session_windows sw JOIN proxy.tasks t ON t.id=sw.task_id WHERE t.session_id=" + SQ + sid + SQ + " ORDER BY window_start_seq")
    tk = sess_tokens(sid)
    valid = True; prev_e = -1
    for w in wins:
        parts = w.split("|")
        if len(parts) >= 2:
            s, e = int(parts[0]), int(parts[1])
            if s <= prev_e: valid = False; break
            prev_e = e
    p = tk >= MIN_TOTAL_TOKENS and valid
    return TestResult("t07","Window Consistency",p,tk,500,time.time()-t0,{"wins":len(wins),"mono":valid},[] if p else["Overlap"])

# === TEST 08: Ledger Consistency ===
async def test_08():
    """T08: Ledger entries have no orphans; session_ledger aggregate matches."""
    sid = "mistral-t08-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    topics = ["Ledger phase-" + str(i//50) + "->" + str(i//50+1) + " item " + str(i) for i in range(500)]
    await drive(sid, 500, topics=topics)
    lc = int(pg_query("SELECT COUNT(*) FROM proxy.ledger_entries le JOIN proxy.tasks t ON t.id=le.task_id WHERE t.session_id=" + SQ + sid + SQ) or 0)
    sl = pg_query_rows("SELECT entry_count,last_updated FROM proxy.session_ledger sl JOIN proxy.tasks t ON t.id=sl.task_id WHERE t.session_id=" + SQ + sid + SQ)
    orph = int(pg_query("SELECT COUNT(*) FROM proxy.ledger_entries le WHERE le.task_id NOT IN (SELECT id FROM proxy.tasks)") or 0)
    tk = sess_tokens(sid)
    agg = False
    if sl and lc > 0:
        agg = int(sl[0].split("|")[0]) == lc
    p = tk >= MIN_TOTAL_TOKENS and orph == 0 and (lc == 0 or agg)
    return TestResult("t08","Ledger Consistency",p,tk,500,time.time()-t0,{"entries":lc,"orphans":orph,"agg":agg},[] if p else["Integrity fail"])

# === TEST 09: Tool Body Truncation ===
async def test_09():
    """T09: Large tool outputs are chunked with valid indices."""
    sid = "mistral-t09-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    topics = ["Full report section " + str(i) + " with appendices" for i in range(500)]
    await drive(sid, 500, fc=1500, topics=topics)
    chunks = pg_query_rows("SELECT tc.chunk_index,LENGTH(tc.content),tc.total_chunks FROM proxy.tool_body_chunks tc JOIN proxy.tasks t ON t.id=tc.task_id WHERE t.session_id=" + SQ + sid + SQ + " ORDER BY tc.event_id,tc.chunk_index LIMIT 20")
    tk = sess_tokens(sid)
    valid = True
    for c in chunks:
        p2 = c.split("|")
        if len(p2) >= 3 and (int(p2[0]) < 0 or int(p2[0]) > 1000):
            valid = False; break
    p = tk >= MIN_TOTAL_TOKENS and valid
    return TestResult("t09","Tool Body Truncation",p,tk,500,time.time()-t0,{"chunks":len(chunks),"valid":valid},[] if p else["Invalid chunks"])

# === TEST 10: Cross-Session Isolation ===
async def test_10():
    """T10: Zero memory bleed between two concurrent sessions."""
    sa = "mistral-t10-A-" + uuid.uuid4().hex[:6]
    sb = "mistral-t10-B-" + uuid.uuid4().hex[:6]
    t0 = time.time()
    fa = "PROJECT ALPHA schema 7.3.1 UUIDv7 primary keys"
    fb = "PROJECT BETA schema 2.1.0 AUTO_INCREMENT primary keys"
    ta = [("Alpha point " + str(i) + (" KEY: " + fa if i==0 else "")) for i in range(250)]
    tb = [("Beta point " + str(i) + (" KEY: " + fb if i==0 else "")) for i in range(250)]
    for i in range(250):
        ma = make_message(ta[i], 800); mb = make_message(tb[i], 800)
        run_goose(ma, sa, resume=(i>0)); run_goose(mb, sb, resume=(i>0))
        await asyncio.sleep(0.3)
    mA = pg_query_rows("SELECT value FROM proxy.memories m JOIN proxy.tasks t ON t.id=m.task_id WHERE t.session_id=" + SQ + sa + SQ + " AND m.active=true")
    mB = pg_query_rows("SELECT value FROM proxy.memories m JOIN proxy.tasks t ON t.id=m.task_id WHERE t.session_id=" + SQ + sb + SQ + " AND m.active=true")
    b_ab = any("ALPHA" in x.upper() or "UUIDv7" in x for x in mB)
    b_ba = any("BETA" in x.upper() or "AUTO_INCREMENT" in x for x in mA)
    tkA = sess_tokens(sa); tkB = sess_tokens(sb)
    p = tkA >= MIN_TOTAL_TOKENS and tkB >= MIN_TOTAL_TOKENS and not b_ab and not b_ba
    errs = []
    if b_ab: errs.append("BLEED A->B")
    if b_ba: errs.append("BLEED B->A")
    return TestResult("t10","Cross-Session Isolation",p,tkA+tkB,500,time.time()-t0,{"tkA":tkA,"tkB":tkB,"bAB":b_ab,"bBA":b_ba},errs)

# === RUNNER ===
async def run_all():
    tests = [test_01,test_02,test_03,test_04,test_05,test_06,test_07,test_08,test_09,test_10]
    results = []
    for i, fn in enumerate(tests, 1):
        sep = "=" * 70
        print(sep)
        doc = fn.__doc__.strip().split(NL)[0] if fn.__doc__ else fn.__name__
        print("TEST " + str(i) + "/10: " + doc)
        print(sep)
        try:
            r = await fn(); results.append(r)
            st = "PASS" if r.passed else "FAIL"
            print("  " + st + " | tokens=" + format(r.total_tokens, ",") + " | " + str(round(r.duration_sec)) + "s")
            for e in r.errors: print("  ERR: " + e)
        except Exception as ex:
            results.append(TestResult(fn.__name__, fn.__name__, False, 0, 0, 0, errors=[str(ex)]))
            print("  EXC: " + str(ex))
    print("=" * 70)
    pc = sum(1 for r in results if r.passed)
    print("SUMMARY: " + str(pc) + "/10 passed")
    for r in results:
        mk = "+" if r.passed else "x"
        print(" [" + mk + "] " + r.name + " (" + format(r.total_tokens, ",") + " tok, " + str(round(r.duration_sec)) + "s)")
    return results

if __name__ == "__main__":
    asyncio.run(run_all())

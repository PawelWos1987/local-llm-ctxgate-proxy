"""ctxgate-dashboard: Health monitoring dashboard for ctxgate-proxy ecosystem.

Monitors: PostgreSQL, vLLM, LM Studio, ctxgate-proxy, 4B worker.
Serves a single-page HTML dashboard on port 9201.
Starts independently; polls service health asynchronously.
"""
import asyncio
import json
import time
import os
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# --- Configuration ---
PORT = int(os.environ.get("CTXGATE_DASHBOARD_PORT", "9201"))
POLL_INTERVAL = float(os.environ.get("CTXGATE_DASHBOARD_POLL", "300.0"))

# Service endpoints to monitor
SERVICES = {
    "postgresql": {"type": "tcp", "host": "127.0.0.1", "port": 5432},
    "vllm": {"type": "http", "url": "http://127.0.0.1:29000/v1/models"},
    "lm_studio": {"type": "http", "url": "http://127.0.0.1:1234/v1/models"},
    "ctxgate_proxy": {"type": "http", "url": "http://127.0.0.1:9200/health"},
    "worker": {"type": "file", "path": "/home/user/ctxproxy/worker/.worker.lock"},
}

# DB for metrics
DB_DSN = os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")

app = FastAPI(title="ctxgate-dashboard")

# Health state (updated by background poller)
health_state: dict = {
    "services": {},
    "db_metrics": {},
    "last_update": None,
}

# --- Health checkers ---
async def check_tcp(host: str, port: int, timeout: float = 2.0) -> dict:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        writer.close()
        await writer.wait_closed()
        return {"status": "up", "latency_ms": None}
    except Exception as e:
        return {"status": "down", "error": str(e)[:100]}

async def check_http(url: str, timeout: float = 5.0) -> dict:
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            latency = int((time.time() - start) * 1000)
            if r.status_code == 200:
                return {"status": "up", "latency_ms": latency}
            return {"status": "degraded", "latency_ms": latency, "http_code": r.status_code}
    except Exception as e:
        return {"status": "down", "error": str(e)[:100]}

async def check_worker_file(path: str) -> dict:
    import re
    try:
        with open(path, "r") as f:
            data = f.read()
        pid_match = re.search(r"pid=(\d+)", data)
        hb_match = re.search(r"heartbeat=([0-9.]+)", data)
        if not pid_match:
            return {"status": "down", "error": "no pid in lock file"}
        pid = int(pid_match.group(1))
        hb = float(hb_match.group(1)) if hb_match else 0
        age = time.time() - hb
        if age < 30:
            return {"status": "up", "pid": pid, "heartbeat_age_s": round(age, 1)}
        elif age < 120:
            return {"status": "degraded", "pid": pid, "heartbeat_age_s": round(age, 1), "error": "stale heartbeat"}
        else:
            return {"status": "down", "pid": pid, "heartbeat_age_s": round(age, 1), "error": "worker frozen/dead"}
    except FileNotFoundError:
        return {"status": "down", "error": "lock file not found"}
    except Exception as e:
        return {"status": "down", "error": str(e)[:100]}

async def get_db_metrics() -> dict:
    """Query PG for operational metrics."""
    try:
        import asyncpg
        conn = await asyncio.wait_for(asyncpg.connect(DB_DSN), timeout=5)
        try:
            tasks = await conn.fetchval("SELECT count(*) FROM proxy.tasks")
            events = await conn.fetchval("SELECT count(*) FROM proxy.events")
            memories = await conn.fetchval("SELECT count(*) FROM proxy.memories WHERE active=true")
            knowledge = await conn.fetchval("SELECT count(*) FROM proxy.knowledge WHERE active=true")
            jobs_pending = await conn.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE status='pending'")
            jobs_processing = await conn.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE status='processing'")
            jobs_done = await conn.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE status='done'")
            jobs_failed = await conn.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE status='failed'")
            return {
                "tasks": tasks, "events": events, "memories": memories,
                "knowledge": knowledge, "jobs_pending": jobs_pending,
                "jobs_processing": jobs_processing, "jobs_done": jobs_done,
                "jobs_failed": jobs_failed,
            }
        finally:
            await conn.close()
    except Exception as e:
        return {"error": str(e)[:100]}

# --- Background poller ---
async def poll_health():
    global health_state
    while True:
        services = {}
        for name, cfg in SERVICES.items():
            if cfg["type"] == "tcp":
                services[name] = await check_tcp(cfg["host"], cfg["port"])
            elif cfg["type"] == "http":
                services[name] = await check_http(cfg["url"])
            elif cfg["type"] == "file":
                services[name] = await check_worker_file(cfg["path"])
        
        db_metrics = await get_db_metrics()
        
        health_state = {
            "services": services,
            "db_metrics": db_metrics,
            "last_update": time.time(),
        }
        await asyncio.sleep(POLL_INTERVAL)

# --- Startup ---
@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_health())

# --- Routes ---
@app.get("/health")
async def health():
    return {"status": "up", "port": PORT, "pid": os.getpid()}

@app.get("/api/health")
async def api_health():
    return JSONResponse(health_state)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML

# --- Dashboard HTML ---
DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>ctxgate-dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Courier New', monospace; background: #0a0a0a; color: #e0e0e0; padding: 20px; }
h1 { color: #4af; margin-bottom: 20px; font-size: 1.4em; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.card { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; padding: 16px; }
.card h3 { font-size: 0.9em; color: #888; margin-bottom: 8px; text-transform: uppercase; }
.status-up { color: #4f4; }
.status-down { color: #f44; }
.status-degraded { color: #fa0; }
.metric { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #222; }
.metric:last-child { border-bottom: none; }
.metric .val { color: #4af; }
.timestamp { color: #555; font-size: 0.8em; margin-top: 16px; }
</style>
</head>
<body>
<h1>ctxgate-dashboard :9201</h1>
<div class="grid">
<div class="card">
<h3>Services</h3>
<div id="services"></div>
</div>
<div class="card">
<h3>Database Metrics</h3>
<div id="dbmetrics"></div>
</div>
</div>
<div class="timestamp" id="ts"></div>
<script>
fetch('/api/health').then(r=>r.json()).then(d=>{
    const svc = document.getElementById('services');
    svc.innerHTML = '';
    for (const [name, info] of Object.entries(d.services||{})) {
        const cls = 'status-' + info.status;
        let extra = '';
        if (info.latency_ms) extra = ' (' + info.latency_ms + 'ms)';
        if (info.heartbeat_age_s) extra = ' (hb ' + info.heartbeat_age_s + 's ago)';
        if (info.error) extra = ' - ' + info.error;
        svc.innerHTML += '<div class="metric"><span>' + name + '</span><span class="' + cls + '">' + info.status + extra + '</span></div>';
    }
    const db = document.getElementById('dbmetrics');
    db.innerHTML = '';
    if (d.db_metrics && !d.db_metrics.error) {
        for (const [k,v] of Object.entries(d.db_metrics)) {
            db.innerHTML += '<div class="metric"><span>' + k + '</span><span class="val">' + v + '</span></div>';
        }
    } else if (d.db_metrics && d.db_metrics.error) {
        db.innerHTML = '<div class="metric"><span>error</span><span class="status-down">' + d.db_metrics.error + '</span></div>';
    }
    document.getElementById('ts').textContent = 'Updated: ' + new Date(d.last_update*1000).toLocaleString();
});
</script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


"""ctxgate-dashboard: Health monitoring + control center for the ctxgate-proxy ecosystem.

Monitors: PostgreSQL, vLLM, LM Studio, ctxgate-proxy, 4B worker.
Serves a single-page GUI on port 9201.
Starts independently (no ordering deps); polls service health asynchronously.

Control: the GUI buttons start / stop / hard-restart the proxy and worker.
Hard-restart = stop -> SIGKILL leftovers -> free the port (kill whatever
holds it) -> reset-failed -> fresh start -> verify health. Zero terminal.
"""
import asyncio
import json
import os
import re
import signal
import time
import threading
import yaml
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import socket
import struct

def _sd_notify(msg: str) -> None:
    """Send sd_notify message to systemd (watchdog keepalive)."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(sock_path)
        s.send(msg.encode())
        s.close()
    except Exception:
        pass

def _watchdog_loop():
    # Fixed 10s ping, independent of POLL_INTERVAL (unit has WatchdogSec=30).
    interval = 10.0
    while True:
        time.sleep(interval)
        _sd_notify("WATCHDOG=1")

# --- Configuration (config.yaml = single source of truth) ---
# config.yaml can be edited from the GUI (SETTINGS button) or externally.
# The dashboard hot-reloads it on mtime change - no restart needed.
CONFIG_PATH = os.environ.get("CTXGATE_CONFIG", "/home/user/ctxproxy/config.yaml")

def load_config() -> dict:
    # Load config.yaml; return {} on error so defaults below apply.
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print("config load error:", e, flush=True)
        return {}

def _get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d

DEFAULT_SERVICES = {
    "postgresql": {"type": "tcp", "host": "127.0.0.1", "port": 5432},
    "vllm": {"type": "http", "url": "http://127.0.0.1:29000/v1/models"},
    "lm_studio": {"type": "http", "url": "http://127.0.0.1:1234/v1/models"},
    "ctxgate_proxy": {"type": "http", "url": "http://127.0.0.1:9200/health"},
    "worker": {"type": "file", "path": "/home/user/ctxproxy/worker/.worker.lock"},
}

def build_services(cfg):
    out = {}
    for name, spec in DEFAULT_SERVICES.items():
        merged = dict(spec)
        merged.update(_get(cfg, "services", name, default={}) or {})
        out[name] = merged
    return out

DEFAULT_SVC_MAP = {
    "ctxgate_proxy": {
        "unit": "ctxproxy-proxy.service",
        "port": 9200,
        "health_url": "http://127.0.0.1:9200/health",
        "kind": "http",
    },
    "worker": {
        "unit": "ctxproxy-worker.service",
        "lock_path": "/home/user/ctxproxy/worker/.worker.lock",
        "kind": "file",
    },
}

def build_svc_map(cfg):
    out = {}
    for name, spec in DEFAULT_SVC_MAP.items():
        merged = dict(spec)
        merged.update(_get(cfg, "control", name, default={}) or {})
        if "port" in merged:
            merged["port"] = int(merged["port"])
        out[name] = merged
    return out

_cfg = load_config()

PORT = int(_get(_cfg, "dashboard", "port", default=9201) or 9201)
POLL_INTERVAL = float(_get(_cfg, "dashboard", "poll_interval", default=3.0) or 3.0)
SERVICES = build_services(_cfg)
DB_DSN = _get(_cfg, "db", "dsn", default=None) or os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")
SVC_MAP = build_svc_map(_cfg)

app = FastAPI(title="ctxgate-dashboard")

# Health state (updated by background poller)
health_state: dict = {
    "services": {},
    "db_metrics": {},
    "last_update": None,
}

# Hot-reload: pick up external edits to config.yaml without restart
_config_mtime = 0.0

def _touch_config_mtime():
    global _config_mtime
    try:
        _config_mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        _config_mtime = 0.0

_touch_config_mtime()

def _maybe_reload_config():
    global _config_mtime, POLL_INTERVAL
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if m == _config_mtime:
        return
    new_cfg = load_config()
    if not new_cfg:
        return
    _config_mtime = m
    globals()["SERVICES"] = build_services(new_cfg)
    globals()["SVC_MAP"] = build_svc_map(new_cfg)
    globals()["DB_DSN"] = _get(new_cfg, "db", "dsn", default=None) or os.environ.get("CTXGATE_DB_DSN", "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")
    globals()["POLL_INTERVAL"] = float(_get(new_cfg, "dashboard", "poll_interval", default=POLL_INTERVAL) or POLL_INTERVAL)
    print("config.yaml reloaded:", CONFIG_PATH, flush=True)

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

# --- Process / port helpers (internal; user only touches the GUI) ---
async def _run(cmd: list, timeout: float = 60.0):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode if proc.returncode is not None else 0
        return (rc, out.decode().strip(), err.decode().strip())
    except FileNotFoundError:
        return (127, "", "command not found: " + cmd[0])
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return (124, "", "timeout: " + cmd[0])

async def _systemctl(*args) -> tuple:
    return await _run(["systemctl", "--user"] + list(args))

async def _port_open(port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=2)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False

async def _port_pid(port: int) -> Optional[int]:
    rc, out, _ = await _run(["ss", "-tlnp", "sport = :" + str(port)])
    for line in out.splitlines():
        m = re.search(r"pid=(\d+)", line)
        if m:
            return int(m.group(1))
    return None

async def _kill_port_forever(port: int, max_wait: float = 30.0) -> tuple:
    """Force-free a port: find the listening PID, SIGKILL it, repeat until free."""
    steps = []
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if not await _port_open(port):
            return True, steps
        pid = await _port_pid(port)
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
                steps.append("SIGKILL pid " + str(pid))
            except ProcessLookupError:
                steps.append("pid " + str(pid) + " already gone")
        else:
            steps.append("port open, no pid via ss")
        await asyncio.sleep(1.0)
    return (not await _port_open(port)), steps

async def _unit_main_pid(unit: str) -> int:
    rc, out, _ = await _systemctl("show", "-p", "MainPID", "--value", unit)
    return int(out) if out.isdigit() else 0

async def _wait_http(url: str, timeout: float = 20.0) -> tuple:
    deadline = time.time() + timeout
    last = "no response"
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(url)
            if r.status_code == 200:
                return True, "HTTP 200"
            last = "HTTP " + str(r.status_code)
        except Exception as e:
            last = str(e)[:60]
        await asyncio.sleep(1.0)
    return False, last

async def _wait_lock_fresh(path: str, timeout: float = 20.0) -> tuple:
    deadline = time.time() + timeout
    last = "lock file missing"
    while time.time() < deadline:
        try:
            with open(path) as f:
                data = f.read()
            m = re.search(r"heartbeat=([0-9.]+)", data)
            if m and (time.time() - float(m.group(1))) < 30:
                return True, "fresh heartbeat"
            last = "stale heartbeat"
        except FileNotFoundError:
            last = "lock file missing"
        except Exception as e:
            last = str(e)[:60]
        await asyncio.sleep(1.0)
    return False, last

# --- Control actions ---
async def _start(name: str) -> dict:
    cfg = SVC_MAP[name]
    steps = []
    # kill any strays first (guarantees one instance)
    if name == "ctxgate_proxy":
        await _kill_all_matching(r"proxy/app\.py")
    else:
        await _kill_all_matching(r"worker/worker\.py")
    if cfg["kind"] == "http" and await _port_open(cfg["port"]):
        rc, state, _ = await _systemctl("show", "-p", "ActiveState", "--value", cfg["unit"])
        if state != "active":
            ok, ksteps = await _kill_port_forever(cfg["port"])
            steps += ksteps
            if not ok:
                return {"ok": False, "error": "port " + str(cfg["port"]) + " still held", "steps": steps}
        else:
            steps.append("already running")
            return {"ok": True, "steps": steps}
    await _systemctl("reset-failed", cfg["unit"])
    rc, out, err = await _systemctl("start", cfg["unit"])
    steps.append("start rc=" + str(rc) + " " + (out or err).strip())
    if rc != 0:
        return {"ok": False, "error": "start failed: " + err, "steps": steps}
    if cfg["kind"] == "http":
        ok, detail = await _wait_http(cfg["health_url"])
    else:
        ok, detail = await _wait_lock_fresh(cfg["lock_path"])
    steps.append("verify: " + detail)
    return {"ok": ok, "steps": steps}

async def _stop(name: str) -> dict:
    cfg = SVC_MAP[name]
    steps = []
    rc, out, err = await _systemctl("stop", cfg["unit"])
    steps.append("stop rc=" + str(rc) + " " + (out or err).strip())
    pid = await _unit_main_pid(cfg["unit"])
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
            steps.append("SIGKILL leftover pid " + str(pid))
        except ProcessLookupError:
            steps.append("pid " + str(pid) + " already gone")
    if cfg["kind"] == "http":
        ok, ksteps = await _kill_port_forever(cfg["port"], max_wait=15.0)
        steps += ksteps
        steps.append("port " + str(cfg["port"]) + ": " + ("free" if ok else "STILL HELD"))
    else:
        try:
            os.remove(cfg["lock_path"])
            steps.append("removed lock file")
        except FileNotFoundError:
            steps.append("no lock file")
    return {"ok": True, "steps": steps}

async def _kill_all_matching(pattern: str, max_wait: float = 10.0) -> list:
    """SIGKILL every process matching pattern. Wait until zero remain."""
    killed = []
    deadline = time.time() + max_wait
    while time.time() < deadline:
        rc, out, _ = await _run(["pkill", "-9", "-f", pattern])
        if out:
            killed.append(out.strip())
        # check if any remain
        rc2, out2, _ = await _run(["pgrep", "-c", "-f", pattern])
        count = out2.strip()
        if count == "" or count == "0":
            break
        await asyncio.sleep(1.0)
    return killed

async def _hard_restart(name: str) -> dict:
    """Kill forever + fresh start: the only way a stuck service ever recovers."""
    cfg = SVC_MAP[name]
    steps = []
    # 1. clean stop (SIGTERM; systemd escalates to SIGKILL on timeout)
    rc, out, err = await _systemctl("stop", cfg["unit"])
    steps.append("stop rc=" + str(rc) + " " + (out or err).strip())
    # 2. SIGKILL ALL matching processes (not just MainPID — kills strays)
    if name == "ctxgate_proxy":
        k = await _kill_all_matching(r"proxy/app\.py")
        steps += ["SIGKILL all: " + x for x in k] or ["no strays"]
    else:
        k = await _kill_all_matching(r"worker/worker\.py")
        steps += ["SIGKILL all: " + x for x in k] or ["no strays"]
    pid = await _unit_main_pid(cfg["unit"])
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
            steps.append("SIGKILL pid " + str(pid))
        except ProcessLookupError:
            steps.append("pid " + str(pid) + " already gone")
    # 3. free the port (proxy) / remove the lock file (worker)
    if cfg["kind"] == "http":
        ok, ksteps = await _kill_port_forever(cfg["port"])
        steps += ksteps
        steps.append("port " + str(cfg["port"]) + ": " + ("free" if ok else "STILL HELD"))
        if not ok:
            return {"ok": False, "error": "port " + str(cfg["port"]) + " could not be freed", "steps": steps}
    else:
        try:
            os.remove(cfg["lock_path"])
            steps.append("removed stale lock file")
        except FileNotFoundError:
            steps.append("no lock file")
    # 4. reset start-limit counter (crash loops would otherwise block a fresh start)
    rc, out, err = await _systemctl("reset-failed", cfg["unit"])
    steps.append("reset-failed rc=" + str(rc))
    # 5. fresh start
    rc, out, err = await _systemctl("start", cfg["unit"])
    steps.append("start rc=" + str(rc) + " " + (out or err).strip())
    if rc != 0:
        return {"ok": False, "error": "fresh start failed: " + err, "steps": steps}
    # 6. verify it is actually healthy
    if cfg["kind"] == "http":
        ok, detail = await _wait_http(cfg["health_url"])
    else:
        ok, detail = await _wait_lock_fresh(cfg["lock_path"])
    steps.append("verify: " + detail)
    return {"ok": ok, "steps": steps}

# --- Background poller ---
async def poll_health():
    _maybe_reload_config()
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
    _sd_notify("READY=1")
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    asyncio.create_task(poll_health())

# --- Routes ---
@app.get("/health")
async def health():
    return {"status": "up", "port": PORT, "pid": os.getpid()}

@app.get("/api/health")
async def api_health():
    return JSONResponse(health_state)

@app.post("/api/control/{name}/{action}")
async def control(name: str, action: str):
    if name not in SVC_MAP:
        return JSONResponse({"ok": False, "error": "unknown service: " + name}, status_code=400)
    if action == "start":
        return JSONResponse(await _start(name))
    if action == "stop":
        return JSONResponse(await _stop(name))
    if action == "restart":
        return JSONResponse(await _hard_restart(name))
    return JSONResponse({"ok": False, "error": "unknown action: " + action}, status_code=400)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML
@app.get("/api/config")
async def get_config():
    _maybe_reload_config()
    try:
        with open(CONFIG_PATH) as f:
            text = f.read()
        return JSONResponse({"ok": True, "path": CONFIG_PATH, "text": text})
    except Exception as e:
        return JSONResponse({"ok": False, "error": "read failed: " + str(e)}, status_code=400)

@app.post("/api/config")
async def put_config(request: Request):
    data = await request.json()
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"ok": False, "error": "body must be {text: <yaml string>}"}, status_code=400)
    try:
        cfg = yaml.safe_load(text)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "invalid YAML: " + str(e)}, status_code=400)
    if not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "YAML must be a mapping"}, status_code=400)
    # Guard: poll interval must stay well below the watchdog timeout.
    try:
        pi = float(_get(cfg, "dashboard", "poll_interval", default=3.0) or 3.0)
        wt = float(_get(cfg, "dashboard", "watchdog_timeout", default=30) or 30)
        if pi >= wt:
            return JSONResponse({"ok": False, "error": "poll_interval (" + str(pi) + "s) must be < watchdog_timeout (" + str(wt) + "s)"}, status_code=400)
    except Exception:
        pass
    try:
        with open(CONFIG_PATH, "w") as f:
            f.write(text)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "write failed: " + str(e)}, status_code=400)
    _touch_config_mtime()
    _maybe_reload_config()
    return JSONResponse({"ok": True, "path": CONFIG_PATH})

@app.get("/api/log")
async def get_log(request: Request):
    p = request.query_params
    n = int(p.get("lines", "400"))
    n = max(1, min(n, 3000))
    source = p.get("source", "dashboard")
    if source == "journal":
        try:
            res = await _run(["journalctl", "--user", "-u", "ctxproxy-dashboard.service", "-n", str(n), "--no-pager"])
            text = res[1] if isinstance(res, (list, tuple)) else str(res)
        except Exception as e:
            return JSONResponse({"ok": False, "source": source, "error": str(e)}, status_code=400)
    else:
        path = os.environ.get("CTXGATE_LOG", "/home/user/ctxproxy/dashboard.log")
        try:
            with open(path) as f:
                lines = f.readlines()
            text = "".join(lines[-n:])
        except Exception as e:
            return JSONResponse({"ok": False, "source": source, "error": "read failed: " + str(e)}, status_code=400)
    return JSONResponse({"ok": True, "source": source, "text": text})

# --- Dashboard HTML (Control Room Pulpit) ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>CTXGATE CONTROL ROOM</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
--bg:#0a0e14;--panel:#0d1520;--border:#1e3a5f;
--green:#00ff88;--yellow:#ffaa00;--red:#ff3344;--blue:#4488ff;
--text:#c8d8e8;--dim:#4a6a8a;
}
body{font-family:'Consolas','Courier New',monospace;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}
.header{background:#080c12;border-bottom:2px solid var(--border);padding:6px 20px;display:flex;justify-content:space-between;align-items:center;height:44px;flex-shrink:0}
.header h1{font-size:1em;color:var(--blue);letter-spacing:4px;text-transform:uppercase}
.header .clock{font-size:.85em;color:var(--dim)}
.main{flex:1;display:grid;grid-template-columns:1fr 1.4fr 1fr;grid-template-rows:1fr 1fr;gap:0;padding:10px;position:relative}
/* Fiber-optic SVG overlay */
.fiber-svg{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2}
/* Waving fiber animation - Mexican flag style */
@keyframes waveGreen{0%{stroke-dashoffset:0}100%{stroke-dashoffset:-60}}
@keyframes waveRed{0%{stroke-dashoffset:0}100%{stroke-dashoffset:60}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.fiber-out{stroke:var(--green);stroke-width:3;fill:none;stroke-dasharray:12 6;animation:waveGreen 1.2s linear infinite;filter:drop-shadow(0 0 4px var(--green))}
.fiber-in{stroke:var(--red);stroke-width:3;fill:none;stroke-dasharray:12 6;animation:waveRed 1.2s linear infinite;filter:drop-shadow(0 0 4px var(--red))}
.fiber-dim{stroke:#1a2a3a;stroke-width:2;fill:none;stroke-dasharray:6 6;opacity:.3}
/* Boxes */
.box{background:var(--panel);border:2px solid var(--border);border-radius:6px;padding:12px;position:relative;z-index:1;display:flex;flex-direction:column;transition:border-color .3s,box-shadow .3s}
.box.up{border-color:var(--green);box-shadow:0 0 15px #00ff8833}
.box.degraded{border-color:var(--yellow);box-shadow:0 0 15px #ffaa0033}
.box.down{border-color:var(--red);box-shadow:0 0 15px #ff334433}
.box .title{font-size:.8em;font-weight:bold;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;display:flex;align-items:center;gap:6px}
.box .led{width:10px;height:10px;border-radius:50%;background:#333;flex-shrink:0}
.box.up .led{background:var(--green);box-shadow:0 0 10px var(--green);animation:pulse 2s infinite}
.box.degraded .led{background:var(--yellow);box-shadow:0 0 10px var(--yellow)}
.box.down .led{background:var(--red);box-shadow:0 0 10px var(--red)}
.box .metrics{flex:1;display:flex;flex-direction:column;gap:3px;font-size:.8em}
.box .metric-row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #111}
.box .metric-row:last-child{border-bottom:none}
.box .val{color:var(--blue);font-weight:bold}
.box .val.big{font-size:1.5em;color:var(--green)}
.box-pg{grid-column:1;grid-row:1}
.box-proxy{grid-column:2;grid-row:1}
.box-vllm{grid-column:3;grid-row:1}
.box-lm{grid-column:3;grid-row:2}
.box-worker{grid-column:2;grid-row:2}
/* Bottom bar */
.bar{background:#080c12;border-top:2px solid var(--border);padding:8px 20px;display:flex;gap:28px;align-items:center;height:44px;flex-shrink:0;font-size:.85em}
.bar .m{display:flex;align-items:center;gap:5px}
.bar .m .l{color:var(--dim);text-transform:uppercase;font-size:.7em;letter-spacing:1px}
.bar .m .n{color:var(--green);font-size:1.2em;font-weight:bold}
.bar .m .n.err{color:var(--red)}
/* Controls */
.controls{display:flex;gap:4px;margin-top:6px;padding-top:6px;border-top:1px solid #111}
.controls button{font-family:inherit;font-size:.65em;font-weight:bold;padding:3px 8px;border-radius:3px;cursor:pointer;text-transform:uppercase;letter-spacing:1px;border:1px solid;transition:all .2s}
.controls button.s{background:#0a2a1a;color:var(--green);border-color:#1a5a3a}
.controls button.s:hover{background:#1a4a2a}
.controls button.p{background:#2a1a0a;color:var(--yellow);border-color:#5a4a1a}
.controls button.p:hover{background:#3a2a1a}
.controls button.r{background:#2a0a0a;color:var(--red);border-color:#5a1a1a}
.controls button.r:hover{background:#3a1a1a}
.controls button:disabled{opacity:.4;cursor:wait}
/* Flow legend */
.legend{position:absolute;bottom:8px;right:20px;z-index:3;font-size:.7em;display:flex;gap:16px}
.legend span{display:flex;align-items:center;gap:4px}
.legend .lg{width:20px;height:3px;background:var(--green);border-radius:2px}
.legend .lr{width:20px;height:3px;background:var(--red);border-radius:2px}
#settingsBtn{background:var(--panel);border:1px solid var(--border);color:var(--blue);font-family:inherit;padding:5px 14px;cursor:pointer;font-size:.85em;letter-spacing:2px;flex-shrink:0}
#settingsBtn:hover{border-color:var(--green);color:var(--green)}
#settingsModal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:100;justify-content:center;align-items:center}
#settingsModal.open{display:flex}
#settingsBox{width:780px;max-width:94vw;max-height:90vh;background:var(--bg);border:2px solid var(--border);display:flex;flex-direction:column}
.sb-head{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel)}
.sb-head h2{font-size:.95em;color:var(--blue);letter-spacing:3px}
.sb-head .close{cursor:pointer;color:var(--red);font-size:1.2em}
.sb-hint{padding:6px 16px;font-size:.72em;color:var(--dim);border-bottom:1px solid var(--border)}
#settingsText{flex:1;min-height:400px;background:#05080d;color:var(--text);border:none;outline:none;padding:14px;font-family:'Consolas','Courier New',monospace;font-size:.85em;resize:none;line-height:1.45}
.sb-foot{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-top:1px solid var(--border);background:var(--panel)}
.sb-foot .msg{font-size:.8em;color:var(--dim)}
.sb-btn{background:var(--panel);border:1px solid var(--border);color:var(--text);font-family:inherit;padding:6px 18px;cursor:pointer;font-size:.85em;letter-spacing:1px;margin-left:8px}
.sb-btn:hover{border-color:var(--blue)}
.sb-btn.save{border-color:var(--green);color:var(--green)}
#logBtn{background:var(--panel);border:1px solid var(--border);color:var(--green);font-family:inherit;padding:5px 14px;cursor:pointer;font-size:.85em;letter-spacing:2px;flex-shrink:0;margin-left:8px}
#logBtn:hover{border-color:var(--green);color:#fff}
#logModal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:110;justify-content:center;align-items:center}
#logModal.open{display:flex}
#logBox{width:960px;max-width:95vw;max-height:92vh;background:var(--bg);border:2px solid var(--border);display:flex;flex-direction:column}
.lg-head{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel)}
.lg-head h2{font-size:.95em;color:var(--green);letter-spacing:3px}
.lg-head .close{cursor:pointer;color:var(--red);font-size:1.2em}
.lg-ctrl{display:flex;gap:16px;align-items:center;padding:7px 16px;border-bottom:1px solid var(--border);font-size:.78em;color:var(--dim)}
.lg-ctrl label{cursor:pointer;user-select:none;display:flex;gap:5px;align-items:center}
.lg-ctrl input[type=checkbox]{accent-color:var(--green)}
.lg-ctrl select{background:var(--panel);color:var(--text);border:1px solid var(--border);font-family:inherit;font-size:.9em;padding:2px 6px}
#lgStat{margin-left:auto;color:var(--dim)}
#logText{flex:1;overflow:auto;background:#05080d;color:var(--text);padding:12px 16px;font-family:'Consolas','Courier New',monospace;font-size:.8em;line-height:1.4;white-space:pre-wrap;word-break:break-word;margin:0}
</style>
</head>
<body>
<div class="header">
    <button id="settingsBtn" onclick="openSettings()">&#9881; SETTINGS</button>
    <button id="logBtn" onclick="openLog()">&#128203; LOGS</button>
<h1>&#9889; CTXGATE CONTROL ROOM</h1>
<div class="clock" id="clock">--:--:--</div>
</div>
<div class="main" id="topology">
<!-- Animated fiber-optic paths -->
<svg class="fiber-svg" id="fibers" viewBox="0 0 1000 600" preserveAspectRatio="none">
<!-- PG to Proxy (input to proxy = RED) -->
<path id="f-pg-proxy" class="fiber-in" d="M 160,150 C 220,100 300,100 370,150"/>
<!-- Proxy to PG (output from proxy = GREEN) -->
<path id="f-proxy-pg" class="fiber-out" d="M 370,200 C 300,250 220,250 160,200"/>
<!-- Proxy to VLLM (output = GREEN) -->
<path id="f-proxy-vllm" class="fiber-out" d="M 580,130 C 640,80 720,80 780,130"/>
<!-- VLLM to Proxy (input = RED) -->
<path id="f-vllm-proxy" class="fiber-in" d="M 780,180 C 720,230 640,230 580,180"/>
<!-- Worker to LM (output = GREEN) -->
<path id="f-worker-lm" class="fiber-out" d="M 580,400 C 640,350 720,350 780,400"/>
<!-- LM to Worker (input = RED) -->
<path id="f-lm-worker" class="fiber-in" d="M 780,450 C 720,500 640,500 580,450"/>
<!-- Worker to PG (output = GREEN) -->
<path id="f-worker-pg" class="fiber-out" d="M 370,420 C 280,380 200,380 160,420"/>
<!-- PG to Worker (input = RED) -->
<path id="f-pg-worker" class="fiber-in" d="M 160,470 C 200,510 280,510 370,470"/>
</svg>

<!-- PostgreSQL -->
<div class="box box-pg" id="box-pg">
<div class="title"><span class="led"></span>POSTGRESQL</div>
<div class="metrics">
<div class="metric-row"><span>Port</span><span class="val">5432</span></div>
<div class="metric-row"><span>Status</span><span class="val" id="pg-status">--</span></div>
<div class="metric-row"><span>Tasks</span><span class="val" id="pg-tasks">--</span></div>
<div class="metric-row"><span>Events</span><span class="val" id="pg-events">--</span></div>
<div class="metric-row"><span>Memories</span><span class="val" id="pg-mem">--</span></div>
<div class="metric-row"><span>Knowledge</span><span class="val" id="pg-know">--</span></div>
</div>
</div>

<!-- CTXGATE Proxy -->
<div class="box box-proxy" id="box-proxy">
<div class="title"><span class="led"></span>CTXGATE PROXY</div>
<div class="metrics">
<div class="metric-row"><span>Port</span><span class="val">:9200</span></div>
<div class="metric-row"><span>Status</span><span class="val" id="px-status">--</span></div>
<div class="metric-row"><span>Latency</span><span class="val big" id="px-lat">--</span></div>
</div>
<div class="controls">
<button class="s" onclick="ctrl('ctxgate_proxy','start')">START</button>
<button class="p" onclick="ctrl('ctxgate_proxy','stop')">STOP</button>
<button class="r" onclick="ctrl('ctxgate_proxy','restart')">RESTART</button>
</div>
</div>

<!-- vLLM -->
<div class="box box-vllm" id="box-vllm">
<div class="title"><span class="led"></span>VLLM 27B</div>
<div class="metrics">
<div class="metric-row"><span>Port</span><span class="val">:29000</span></div>
<div class="metric-row"><span>Status</span><span class="val" id="vl-status">--</span></div>
<div class="metric-row"><span>Latency</span><span class="val big" id="vl-lat">--</span></div>
</div>
</div>

<!-- LM Studio -->
<div class="box box-lm" id="box-lm">
<div class="title"><span class="led"></span>LM STUDIO 4B</div>
<div class="metrics">
<div class="metric-row"><span>Port</span><span class="val">:1234</span></div>
<div class="metric-row"><span>Status</span><span class="val" id="lm-status">--</span></div>
<div class="metric-row"><span>Latency</span><span class="val big" id="lm-lat">--</span></div>
</div>
</div>

<!-- Worker -->
<div class="box box-worker" id="box-worker">
<div class="title"><span class="led"></span>WORKER 4B</div>
<div class="metrics">
<div class="metric-row"><span>PID</span><span class="val" id="wk-pid">--</span></div>
<div class="metric-row"><span>Status</span><span class="val" id="wk-status">--</span></div>
<div class="metric-row"><span>Heartbeat</span><span class="val big" id="wk-hb">--</span></div>
</div>
<div class="controls">
<button class="s" onclick="ctrl('worker','start')">START</button>
<button class="p" onclick="ctrl('worker','stop')">STOP</button>
<button class="r" onclick="ctrl('worker','restart')">RESTART</button>
</div>
</div>

<!-- Legend -->
<div class="legend">
<span><span class="lg"></span> OUTPUT</span>
<span><span class="lr"></span> INPUT</span>
</div>
</div>

<!-- Bottom metrics bar -->
<div class="bar">
<div class="m"><span class="l">Pending</span><span class="n" id="m-pend">0</span></div>
<div class="m"><span class="l">Processing</span><span class="n" id="m-proc">0</span></div>
<div class="m"><span class="l">Done</span><span class="n" id="m-done">0</span></div>
<div class="m"><span class="l">Failed</span><span class="n" id="m-fail">0</span></div>
<div class="m"><span class="l">Tasks</span><span class="n" id="m-tasks">0</span></div>
<div class="m"><span class="l">Events</span><span class="n" id="m-evt">0</span></div>
<div class="m"><span class="l">Update</span><span class="n" id="m-upd" style="font-size:.85em">--</span></div>
</div>

<div id="settingsModal">
  <div id="settingsBox">
    <div class="sb-head"><h2>&#9881; SETTINGS — config.yaml</h2><span class="close" onclick="closeSettings()">&#10005;</span></div>
    <div class="sb-hint">All runtime parameters (ports, URLs, DSN, poll interval, watchdog, service units). Edit the YAML and press SAVE. File: /home/user/ctxproxy/config.yaml — also editable externally; changes are hot-reloaded, no restart needed.</div>
    <textarea id="settingsText" spellcheck="false"></textarea>
    <div class="sb-foot">
      <span class="msg" id="settingsMsg"></span>
      <div><button class="sb-btn" onclick="reloadSettingsText()">RELOAD</button><button class="sb-btn save" onclick="saveSettings()">SAVE</button></div>
    </div>
  </div>
</div>
<div id="logModal">
  <div id="logBox">
    <div class="lg-head"><h2>&#128203; LOG EXPLORER</h2><span class="close" onclick="closeLog()">&#10005;</span></div>
    <div class="lg-ctrl">
      <label><input type="checkbox" id="lgAuto" checked> AUTOSCROLL</label>
      <label>SOURCE
        <select id="lgSrc" onchange="loadLog()">
          <option value="dashboard">dashboard.log</option>
          <option value="journal">journalctl (systemd)</option>
        </select>
      </label>
      <span id="lgStat"></span>
    </div>
    <pre id="logText"></pre>
  </div>
</div>
<script>
function $(id){return document.getElementById(id)}
function setBox(id,st){var b=$('box-'+id);if(!b)return;b.className=b.className.replace(/\b(up|down|degraded)\b/g,'').trim();if(st==='up')b.classList.add('up');else if(st==='degraded')b.classList.add('degraded');else b.classList.add('down');}
function sv(id,t){var e=$(id);if(e)e.textContent=t||'--'}
async function poll(){
try{
var r=await fetch('/api/health');var d=await r.json();
var s=d.services||{};var db=d.db_metrics||{};
setBox('pg',s.postgresql?s.postgresql.status:'down');
sv('pg-status',s.postgresql?s.postgresql.status.toUpperCase():'DOWN');
sv('pg-tasks',db.tasks!=null?db.tasks:'--');
sv('pg-events',db.events!=null?db.events:'--');
sv('pg-mem',db.memories!=null?db.memories:'--');
sv('pg-know',db.knowledge!=null?db.knowledge:'--');
setBox('proxy',s.ctxgate_proxy?s.ctxgate_proxy.status:'down');
sv('px-status',s.ctxgate_proxy?s.ctxgate_proxy.status.toUpperCase():'DOWN');
sv('px-lat',s.ctxgate_proxy&&s.ctxgate_proxy.latency_ms!=null?s.ctxgate_proxy.latency_ms+'ms':'--');
setBox('vllm',s.vllm?s.vllm.status:'down');
sv('vl-status',s.vllm?s.vllm.status.toUpperCase():'DOWN');
sv('vl-lat',s.vllm&&s.vllm.latency_ms!=null?s.vllm.latency_ms+'ms':'--');
setBox('lm',s.lm_studio?s.lm_studio.status:'down');
sv('lm-status',s.lm_studio?s.lm_studio.status.toUpperCase():'DOWN');
sv('lm-lat',s.lm_studio&&s.lm_studio.latency_ms!=null?s.lm_studio.latency_ms+'ms':'--');
setBox('worker',s.worker?s.worker.status:'down');
sv('wk-status',s.worker?s.worker.status.toUpperCase():'DOWN');
sv('wk-pid',s.worker&&s.worker.pid?s.worker.pid:'--');
sv('wk-hb',s.worker&&s.worker.heartbeat_age_s!=null?s.worker.heartbeat_age_s+'s':'--');
sv('m-pend',db.jobs_pending!=null?db.jobs_pending:0);
sv('m-proc',db.jobs_processing!=null?db.jobs_processing:0);
sv('m-done',db.jobs_done!=null?db.jobs_done:0);
sv('m-fail',db.jobs_failed!=null?db.jobs_failed:0);
sv('m-tasks',db.tasks!=null?db.tasks:0);
sv('m-evt',db.events!=null?db.events:0);
if(d.last_update)sv('m-upd',new Date(d.last_update*1000).toLocaleTimeString());
var fe=$('m-fail');if(db.jobs_failed>0)fe.className='n err';else fe.className='n';
// Animate fibers based on health
setFiber('f-pg-proxy',s.postgresql,s.ctxgate_proxy);
setFiber('f-proxy-pg',s.ctxgate_proxy,s.postgresql);
setFiber('f-proxy-vllm',s.ctxgate_proxy,s.vllm);
setFiber('f-vllm-proxy',s.vllm,s.ctxgate_proxy);
setFiber('f-worker-lm',s.worker,s.lm_studio);
setFiber('f-lm-worker',s.lm_studio,s.worker);
setFiber('f-worker-pg',s.worker,s.postgresql);
setFiber('f-pg-worker',s.postgresql,s.worker);
}catch(e){console.error(e)}
}
function setFiber(id,s1,s2){
var el=$(id);if(!el)return;
var st1=s1?s1.status:'down';
var st2=s2?s2.status:'down';
if(st1==='up'&&st2==='up'){
el.style.display='block';
}else{
el.style.display='block';
el.classList.remove('fiber-out','fiber-in');
el.classList.add('fiber-dim');
}
}
async function ctrl(n,a){
var b=event.target;b.disabled=true;
try{var r=await fetch('/api/control/'+n+'/'+a,{method:'POST'});var d=await r.json();if(!d.ok)alert('Err: '+(d.error||'?'))}
catch(e){alert('Ctrl: '+e.message)}
b.disabled=false;
}
function tick(){$('clock').textContent=new Date().toLocaleTimeString()}
tick();setInterval(tick,1000);
poll();setInterval(poll,3000);
function openSettings(){
  document.getElementById('settingsModal').classList.add('open');
  const msg = document.getElementById('settingsMsg');
  const ta = document.getElementById('settingsText');
  msg.textContent = 'Loading config.yaml ...';
  msg.style.color = '';
  fetch('/api/config').then(r=>r.json()).then(d=>{
    if(d.ok){ ta.value = d.text; msg.textContent = d.path; }
    else { msg.textContent = 'Error: ' + (d.error||''); }
  }).catch(e=>{ msg.textContent = 'Fetch failed: ' + e; });
}
function closeSettings(){ document.getElementById('settingsModal').classList.remove('open'); }
function reloadSettingsText(){ openSettings(); }
function saveSettings(){
  const msg = document.getElementById('settingsMsg');
  const ta = document.getElementById('settingsText');
  msg.textContent = 'Saving ...';
  msg.style.color = '';
  fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text: ta.value})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){ msg.textContent = 'Saved: ' + d.path + ' (hot-reloaded)'; msg.style.color = 'var(--green)'; }
      else { msg.textContent = 'YAML error: ' + d.error; msg.style.color = 'var(--red)'; }
      setTimeout(()=>{ msg.style.color = ''; }, 5000);
    }).catch(e=>{ msg.textContent = 'Save failed: ' + e; msg.style.color = 'var(--red)'; });
}
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeSettings(); });
let lgTimer = null;
function openLog(){
  document.getElementById('logModal').classList.add('open');
  loadLog();
  if(lgTimer) clearInterval(lgTimer);
  lgTimer = setInterval(function(){ if(document.getElementById('logModal').classList.contains('open')) loadLog(); }, 2000);
}
function closeLog(){
  document.getElementById('logModal').classList.remove('open');
  if(lgTimer){ clearInterval(lgTimer); lgTimer = null; }
}
function loadLog(){
  var src = document.getElementById('lgSrc').value;
  fetch('/api/log?source=' + src + '&lines=500').then(function(r){return r.json();}).then(function(d){
    var el = document.getElementById('logText');
    var stat = document.getElementById('lgStat');
    if(d.ok){
      var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 60;
      var changed = (el._last !== d.text);
      el.textContent = d.text;
      el._last = d.text;
      if(document.getElementById('lgAuto').checked && (atBottom || changed)){
        el.scrollTop = el.scrollHeight;
      }
      stat.textContent = 'updated ' + new Date().toLocaleTimeString();
    } else {
      stat.textContent = 'error: ' + (d.error||'unknown');
    }
  }).catch(function(e){ document.getElementById('lgStat').textContent = 'fetch failed: ' + e; });
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

"""ctxgate-dashboard: Health monitoring + control center for the ctxgate-proxy ecosystem.

Monitors: PostgreSQL, vLLM, LM Studio, ctxgate-proxy, 4B worker.
Serves a single-page GUI on port 9202.
Starts independently (no ordering deps); polls service health asynchronously.

Control: the GUI buttons start / stop / restart the proxy and worker.
  - start   → systemctl --user start  (fresh launch)
  - stop    → systemctl --user stop   (SIGTERM → proxy drains in-flight streams)
  - restart → systemctl --user restart (graceful drain + relaunch)
All control actions go through systemd user units (ctxgate-proxy.service,
ctxgate-worker.service).  No pkill / fuser / kill-by-pattern.

System metrics (htop-style): CPU, memory, swap, load average, network
throughput, per-process CPU+MEM — all read from /proc (zero external deps).
"""
import asyncio
import logging
import logging.handlers
import os
import sys
import re
import signal
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import yaml

# ── Logging with rotation (10 MB × 5 backups) ──────────────────────────
_LOG_PATH = os.environ.get("CTXGATE_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard.log"))
_log_handler = logging.handlers.RotatingFileHandler(_LOG_PATH, maxBytes=10*1024*1024, backupCount=5)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
_log = logging.getLogger("dashboard")
_log.setLevel(logging.INFO)
_log.addHandler(_log_handler)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
_log.addHandler(_stream_handler)

import socket

from collections import deque

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse


# ── systemd integration ────────────────────────────────────────────────
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
    """Fixed 10 s watchdog ping (unit has WatchdogSec=90)."""
    interval = 10.0
    while not _shutdown_event.is_set():
        _shutdown_event.wait(interval)
        if _shutdown_event.is_set():
            break
        _sd_notify("WATCHDOG=1")


# ── Configuration (config.yaml = single source of truth) ───────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
CONFIG_PATH = os.environ.get("CTXGATE_CONFIG", os.path.join(_ROOT, "config.yaml"))
ENV_PATH = os.environ.get("CTXGATE_ENV_FILE", "/etc/ctxgate-proxy/env")


def _validate_env_text(text: str) -> tuple[bool, str]:
    """Return (ok, error). Reject malformed lines so a bad save cannot brick the units."""
    if not text.strip():
        return False, "env file is empty"
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            return False, f"line {i}: missing '=' (got: {line[:60]!r})"
        key, _, _val = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            return False, f"line {i}: invalid variable name {key!r}"
    return True, ""


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        _log.warning("config load error: %s", e)
        return {}


def _get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


# ── Service definitions ────────────────────────────────────────────────
DEFAULT_SERVICES = {
    "postgresql": {"type": "pg"},
    "vllm":       {"type": "http", "url": "http://127.0.0.1:29000/v1/models"},
    "mistral":    {"type": "http", "url": "http://127.0.0.1:9201/api/lmstudio"},
    "ctxgate_proxy": {"type": "http", "url": "http://127.0.0.1:9201/health"},
    "worker":     {"type": "file", "path": os.path.join(_ROOT, "worker", ".worker.lock")},
}


def build_services(cfg):
    out = {}
    for name, spec in DEFAULT_SERVICES.items():
        merged = dict(spec)
        merged.update(_get(cfg, "services", name, default={}) or {})
        out[name] = merged
    return out


# ── Control map: which systemd unit manages each controllable service ──
# IMPORTANT: unit names must match the actual .service files installed in
# ~/.config/systemd/user/.  The old code had "ctxproxy-*" which did not
# exist; the real units are "ctxgate-*".
DEFAULT_SVC_MAP = {
    "ctxgate_proxy": {
        "unit": "ctxgate-proxy.service",
        "port": 9201,
        "health_url": "http://127.0.0.1:9201/health",
        "kind": "http",
    },
    "worker": {
        "unit": "ctxgate-worker.service",
        "lock_path": os.path.join(_ROOT, "worker", ".worker.lock"),
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

PORT = int(os.environ.get("CTXGATE_DASHBOARD_PORT")
          or _get(_cfg, "dashboard", "port", default=9202) or 9202)
POLL_INTERVAL = float(_get(_cfg, "dashboard", "poll_interval", default=3.0) or 3.0)
SERVICES = build_services(_cfg)
DB_DSN = (os.environ.get("CTXGATE_DB_DSN")
          or os.environ.get("CTXPROXY_DB_DSN")
          or _get(_cfg, "db", "dsn", default=None)
          or "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")
SVC_MAP = build_svc_map(_cfg)

_pg_pool = None
_last_config_write = 0.0
_shutdown_event = threading.Event()
HOST = os.environ.get("CTXGATE_DASHBOARD_HOST", "127.0.0.1")
DASH_TOKEN = os.environ.get("CTXGATE_DASHBOARD_TOKEN", "")


def _auth_ok(request) -> bool:
    if not DASH_TOKEN:
        return True
    return request.headers.get("Authorization", "") == "Bearer " + DASH_TOKEN


# ── Lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(_app):
    _sd_notify("READY=1")
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    try:
        import asyncpg
        if DB_DSN:
            global _pg_pool
            _pg_pool = await asyncpg.create_pool(
                DB_DSN, min_size=1, max_size=3,
                command_timeout=5, max_inactive_connection_lifetime=300)
    except Exception as e:
        _log.warning("PG pool init failed: %s", e)
    asyncio.create_task(poll_health())
    yield
    _shutdown_event.set()
    if _pg_pool is not None:
        try:
            await _pg_pool.close()
        except Exception:
            pass


app = FastAPI(title="ctxgate-dashboard", lifespan=_lifespan)

# ── Health state (updated by background poller) ────────────────────────
health_state: dict = {
    "services": {},
    "db_metrics": {},
    "system": {},
    "vllm_metrics": {},
    "gpu": {},
    "process_util": {},
    "util_history": [],
    "last_update": None,
}

# ── Hot-reload config.yaml on mtime change ─────────────────────────────
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
    new_dsn = (os.environ.get("CTXGATE_DB_DSN")
               or os.environ.get("CTXPROXY_DB_DSN")
               or _get(new_cfg, "db", "dsn", default=None)
               or "postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy")
    if new_dsn != globals().get("DB_DSN"):
        _log.warning("DSN changed – pool will use new DSN on next restart.")
    globals()["DB_DSN"] = new_dsn
    globals()["POLL_INTERVAL"] = float(
        _get(new_cfg, "dashboard", "poll_interval", default=POLL_INTERVAL) or POLL_INTERVAL)
    _log.info("config.yaml reloaded: %s", CONFIG_PATH)


# ── Health checkers ────────────────────────────────────────────────────
async def check_tcp(host: str, port: int, timeout: float = 2.0) -> dict:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
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
            return {"status": "degraded", "latency_ms": latency,
                    "http_code": r.status_code}
    except Exception as e:
        return {"status": "down", "error": str(e)[:100]}


async def check_pg() -> dict:
    """Run SELECT 1 against PG to verify actual queryability."""
    try:
        if _pg_pool is not None:
            async with _pg_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return {"status": "up"}
        import asyncpg
        conn = await asyncio.wait_for(asyncpg.connect(DB_DSN), timeout=5)
        try:
            await conn.fetchval("SELECT 1")
            return {"status": "up"}
        finally:
            await conn.close()
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
            result = {"status": "up", "pid": pid, "heartbeat_age_s": round(age, 1)}
        elif age < 120:
            result = {"status": "degraded", "pid": pid,
                    "heartbeat_age_s": round(age, 1), "error": "stale heartbeat"}
        else:
            result = {"status": "down", "pid": pid,
                    "heartbeat_age_s": round(age, 1), "error": "worker frozen/dead"}
        # Merge throttling data from the status JSON file
        status_path = path.replace(".worker.lock", ".worker_status.json")
        try:
            with open(status_path, "r") as f2:
                sdata = json.loads(f2.read())
            result["lm_rpm"] = sdata.get("lm_rpm", 0)
            result["lm_latency_ms"] = sdata.get("lm_latency_ms", 0)
            result["lm_ctx_avg"] = sdata.get("lm_ctx_avg", 0)
            result["lm_ctx_max"] = sdata.get("lm_ctx_max", 0)
            result["lm_tokens_in"] = sdata.get("lm_tokens_in_total", 0)
            result["lm_tokens_out"] = sdata.get("lm_tokens_out_total", 0)
        except Exception:
            pass
        return result
    except FileNotFoundError:
        return {"status": "down", "error": "lock file not found"}
    except Exception as e:
        return {"status": "down", "error": str(e)[:100]}

# ── vLLM engine metrics (real data from Prometheus /metrics) ─────────
# Computes 5-minute rolling rates from vLLM's cumulative counters and
# gauges.  "Real" generation speed is derived from the inter-token
# latency histogram (1 / mean seconds-per-output-token), not from a raw
# token counter, so it reflects actual decode speed.
VLLM_WINDOW = 300.0  # 5 minutes
_vllm_samples: "deque" = deque(maxlen=2000)


def _vllm_metrics_url() -> str:
    u = SERVICES.get("vllm", {}).get("url", "http://127.0.0.1:29000/v1/models")
    try:
        m = re.match(r"(https?://[^/]+)", u)
        base = m.group(1) if m else "http://127.0.0.1:29000"
        return base + "/metrics"
    except Exception:
        return "http://127.0.0.1:29000/metrics"


async def check_vllm_metrics(url: str, window: float = VLLM_WINDOW) -> dict:
    """Fetch vLLM /metrics and compute 5-minute rolling rates (real data)."""
    now = time.time()
    out: dict = {"status": "down", "window_s": window}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            text = r.text
    except Exception as e:
        out["error"] = str(e)[:100]
        return out
    out["status"] = "up"

    lines = text.splitlines()

    def _sum(name: str) -> float:
        tot = 0.0
        for ln in lines:
            if ln.startswith(name + "{") and not ln.startswith(name + "_"):
                try:
                    tot += float(ln.rsplit(" ", 1)[1])
                except Exception:
                    pass
        return tot

    def _gauge(name: str) -> Optional[float]:
        val = None
        for ln in lines:
            if ln.startswith(name + "{") or ln.startswith(name + " "):
                try:
                    val = float(ln.rsplit(" ", 1)[1])
                except Exception:
                    pass
        return val

    prompt_t = _sum("vllm:prompt_tokens_total")
    gen_t = _sum("vllm:generation_tokens_total")
    pc_q = _sum("vllm:prefix_cache_queries_total")
    pc_h = _sum("vllm:prefix_cache_hits_total")
    kv = _gauge("vllm:kv_cache_usage_perc")
    running = _gauge("vllm:num_requests_running")
    waiting = _gauge("vllm:num_requests_waiting")
    tok_sum = 0.0
    tok_cnt = 0.0
    for ln in lines:
        if ln.startswith("vllm:request_time_per_output_token_seconds_sum{"):
            try:
                tok_sum = float(ln.rsplit(" ", 1)[1])
            except Exception:
                pass
        elif ln.startswith("vllm:request_time_per_output_token_seconds_count{"):
            try:
                tok_cnt = float(ln.rsplit(" ", 1)[1])
            except Exception:
                pass

    sample = {"t": now, "prompt": prompt_t, "gen": gen_t, "pc_q": pc_q,
              "pc_h": pc_h, "kv": kv, "running": running, "waiting": waiting,
              "tok_sum": tok_sum, "tok_cnt": tok_cnt}
    _vllm_samples.append(sample)
    while _vllm_samples and (now - _vllm_samples[0]["t"]) > window:
        _vllm_samples.popleft()

    cur = _vllm_samples[-1]
    old = _vllm_samples[0]
    dt = max(1.0, cur["t"] - old["t"])
    out["dt_s"] = round(dt, 1)

    def _rate(cv: float, ov: float) -> float:
        return round((cv - ov) / dt, 1)

    out["prefill_tps"] = _rate(cur["prompt"], old["prompt"])
    out["gen_total_tps"] = _rate(cur["gen"], old["gen"])
    d_tok_sum = cur["tok_sum"] - old["tok_sum"]
    d_tok_cnt = cur["tok_cnt"] - old["tok_cnt"]
    out["gen_real_tps"] = (round(1.0 / (d_tok_sum / d_tok_cnt), 1)
                           if d_tok_cnt > 0 and d_tok_sum > 0 else None)
    d_q = cur["pc_q"] - old["pc_q"]
    d_h = cur["pc_h"] - old["pc_h"]
    out["prefix_hit_rate"] = round(d_h / d_q, 4) if d_q > 0 else None
    out["kv_cache_pct"] = round(kv * 100, 1) if kv is not None else None
    out["running"] = int(running) if running is not None else None
    out["waiting"] = int(waiting) if waiting is not None else None
    out["samples"] = len(_vllm_samples)
    return out


# ── System metrics (htop-style, all from /proc — zero external deps) ───
# We read the kernel's own counters so the dashboard never needs prometheus,
# node_exporter, or any other agent.  Each sample is a single dict; the poller
# keeps a short ring buffer for deltas (CPU %, net throughput, disk I/O).

def _read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def _cpu_times() -> list:
    """Return the aggregate CPU jiffies from /proc/stat (first 'cpu' line).
    Order: user nice system idle iowait irq softirq steal guest guest_nice"""
    line = _read_file("/proc/stat").splitlines()[0] if _read_file("/proc/stat") else ""
    if not line.startswith("cpu "):
        return [0] * 10
    return [int(x) for x in line.split()[1:11]]


def _cpu_times_per_core() -> list:
    """Return per-core jiffies from /proc/stat (cpu0..cpuN lines).
    Each entry: [user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice]"""
    cores = []
    for ln in _read_file("/proc/stat").splitlines():
        if ln.startswith("cpu") and ln[3:4].isdigit():
            parts = ln.split()
            cores.append([int(x) for x in parts[1:11]])
    return cores


def _vmstat_counters() -> dict:
    """Cheap memory I/O counters from /proc/vmstat (cumulative).
    pgpgin/pgpgout = pages read/written to disk (4KB pages).
    pswpin/pswpout = swap pages in/out.
    pgfault/pgmajfault = minor/major page faults."""
    out = {}
    for ln in _read_file("/proc/vmstat").splitlines():
        parts = ln.split()
        if len(parts) == 2:
            out[parts[0]] = int(parts[1])
    return out


def _psi() -> dict:
    """Pressure Stall Information from /proc/pressure/{memory,io,cpu}.
    Returns some/full avg10 (percent of time stalled)."""
    result = {}
    for resource in ("memory", "io", "cpu"):
        try:
            lines = _read_file("/proc/pressure/" + resource).splitlines()
            some = full = 0.0
            for ln in lines:
                if ln.startswith("some"):
                    some = float(ln.split("avg10=")[1].split()[0])
                elif ln.startswith("full"):
                    full = float(ln.split("avg10=")[1].split()[0])
            result[resource] = {"some": some, "full": full}
        except Exception:
            result[resource] = {"some": 0.0, "full": 0.0}
    return result


def _meminfo() -> dict:
    out = {}
    for ln in _read_file("/proc/meminfo").splitlines():
        k, _, rest = ln.partition(":")
        out[k.strip()] = int(rest.strip().split()[0]) if rest.strip() else 0  # kB
    return out


def _netstat_bytes() -> dict:
    """Sum RX/TX bytes across all non-loopback interfaces from /proc/net/dev."""
    rx = tx = 0
    for ln in _read_file("/proc/net/dev").splitlines()[2:]:
        name, _, rest = ln.partition(":")
        if not name.strip() or name.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) >= 9:
            rx += int(fields[0])   # bytes received
            tx += int(fields[8])   # bytes transmitted
    return {"rx": rx, "tx": tx}


def _diskio_bytes() -> dict:
    """Sum sectors read+written from /proc/diskstats (all real devices)."""
    rd = wr = 0
    for ln in _read_file("/proc/diskstats").splitlines():
        parts = ln.split()
        if len(parts) >= 10 and (parts[2].startswith("sd")
                                 or parts[2].startswith("nvme")
                                 or parts[2].startswith("vd")
                                 or parts[2].startswith("mmc")):
            rd += int(parts[5]) * 512   # sectors read
            wr += int(parts[9]) * 512   # sectors written
    return {"rd": rd, "wr": wr}


def _loadavg() -> tuple:
    la = _read_file("/proc/loadavg").split()
    if len(la) >= 3:
        return (float(la[0]), float(la[1]), float(la[2]))
    return (0.0, 0.0, 0.0)


_proc_prev_ticks: dict = {}


def _parse_core_range(s: str) -> tuple:
    """'0-3' -> (4, '0-3'); '0-31' -> (32, '0-31'); '0,2,5' -> (3, '0,2,5')."""
    s = (s or "").strip()
    if not s:
        return (1, "all")
    total = 0
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            total += int(b) - int(a) + 1
        elif part:
            total += 1
    return (total, s)


def _proc_snapshot() -> list:
    """Per-process: real CPU% (tick delta), memory, and PER-CORE load.
    per_core% = total% / #allowed_cores, so '0-3 (95%)' means 4 pinned cores
    each ~95% - the signal for pinning / parallelism decisions.  Sorted by RSS."""
    now = time.time()
    rows = []
    seen = set()
    try:
        pids = os.listdir("/proc")
    except Exception:
        return rows
    total_mem_kb = _meminfo().get("MemTotal", 1)
    ncpu = os.cpu_count() or 1
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            stat = open("/proc/" + pid + "/stat").read()
            rp = stat.rfind(")")
            fields = stat[rp + 2:].split()
            utime = int(fields[11]); stime = int(fields[12])
            rss_pages = int(fields[21])
            name = stat[stat.index("(") + 1:rp]
            # Derive a better name from cmdline for python processes
            _cmd = ""
            try:
                _cmd = open("/proc/" + pid + "/cmdline").read().replace("\0", " ")
            except Exception:
                pass
            if name in ("python", "python3") and _cmd:
                _parts = _cmd.split()
                for _p in _parts:
                    if _p.endswith(".py"):
                        name = _p.rsplit("/", 1)[-1].replace(".py", "")
                        break
            rss_kb = rss_pages * 4
            ticks = utime + stime
            cores_list = "0-" + str(ncpu - 1)
            threads = 1
            try:
                st = open("/proc/" + pid + "/status").read()
                for ln in st.splitlines():
                    if ln.startswith("Cpus_allowed_list:"):
                        cores_list = ln.split(":", 1)[1].strip()
                    elif ln.startswith("Threads:"):
                        threads = int(ln.split(":", 1)[1].strip())
            except Exception:
                pass
            core_count, core_range = _parse_core_range(cores_list)
            prev = _proc_prev_ticks.get(pid)
            if prev:
                dt = max(0.05, now - prev["ts"])
                cpu_pct = 100.0 * (max(0, ticks - prev["ticks"]) / _CLK_TCK) / dt
            else:
                cpu_pct = 0.0
            _proc_prev_ticks[pid] = {"ticks": ticks, "ts": now}
            seen.add(int(pid))
            per_core = min(100.0, cpu_pct / max(1, core_count))
            rows.append({
                "pid": int(pid),
                "name": name[:20],
                "cmdline": _cmd[:120] if '_cmd' in dir() else "",
                "rss_kb": rss_kb,
                "mem_pct": round(rss_kb / total_mem_kb * 100, 1),
                "cpu_pct": round(cpu_pct, 1),
                "core_range": core_range,
                "core_count": core_count,
                "per_core_pct": round(per_core, 1),
                "threads": threads,
            })
        except Exception:
            continue
    # prune pids that vanished
    for p in [x for x in _proc_prev_ticks if x not in seen]:
        del _proc_prev_ticks[p]
    # Filter to model-flow processes only
    _FLOW = ('vllm', 'nexus', 'goose', 'proxy', 'worker', 'dashboard', 'postgres', 'app.py', 'worker.py', 'dashboard.py')
    def _is_flow(r: dict) -> bool:
        s = (r['name'] + ' ' + r.get('cmdline', '')).lower()
        return any(p in s for p in _FLOW)
    rows = [r for r in rows if _is_flow(r)]
    rows.sort(key=lambda r: r["rss_kb"], reverse=True)
    return rows[:12]


def _uptime() -> float:
    u = _read_file("/proc/uptime").split()
    return float(u[0]) if u else 0.0


# Ring buffer for deltas. _prev holds the previous sample's counters.
_sys_prev = {"cpu": None, "cpu_cores": None, "net": None, "disk": None, "vmstat": None, "psi": None, "ts": 0.0}


def sample_system() -> dict:
    """One full htop-style sample. Computes deltas vs. the previous call so
    the returned percentages/throughputs are real rates, not cumulative sums."""
    now = time.time()
    cpu = _cpu_times()
    cpu_cores = _cpu_times_per_core()
    mem = _meminfo()
    net = _netstat_bytes()
    disk = _diskio_bytes()
    load = _loadavg()
    vmstat = _vmstat_counters()
    psi = _psi()

    total = sum(cpu)
    idle = cpu[3] + cpu[4]
    dt = (now - _sys_prev["ts"]) if _sys_prev["ts"] else 1.0
    dt = max(dt, 0.05)

    # -- Aggregate CPU --
    if _sys_prev["cpu"] is not None:
        prev_total = sum(_sys_prev["cpu"])
        prev_idle = _sys_prev["cpu"][3] + _sys_prev["cpu"][4]
        dtick = total - prev_total
        didle = idle - prev_idle
        cpu_pct = round(100.0 * (1 - didle / dtick), 1) if dtick > 0 else 0.0
    else:
        cpu_pct = 0.0

    # -- Per-core CPU (first 8 cores for display) --
    per_core_pct = [0.0] * 8
    if _sys_prev["cpu_cores"] is not None:
        for i in range(min(8, len(cpu_cores), len(_sys_prev["cpu_cores"]))):
            cur = cpu_cores[i]
            prev = _sys_prev["cpu_cores"][i]
            ct = sum(cur)
            pt = sum(prev)
            ci = cur[3] + cur[4]
            pi = prev[3] + prev[4]
            dtc = ct - pt
            di = ci - pi
            per_core_pct[i] = round(100.0 * (1 - di / dtc), 1) if dtc > 0 else 0.0

    # -- Network --
    if _sys_prev["net"] is not None:
        net_rx = round((net["rx"] - _sys_prev["net"]["rx"]) / dt / 1024, 1)
        net_tx = round((net["tx"] - _sys_prev["net"]["tx"]) / dt / 1024, 1)
    else:
        net_rx = net_tx = 0.0

    # -- Disk --
    if _sys_prev["disk"] is not None:
        disk_rd = round((disk["rd"] - _sys_prev["disk"]["rd"]) / dt / 1024, 1)
        disk_wr = round((disk["wr"] - _sys_prev["disk"]["wr"]) / dt / 1024, 1)
    else:
        disk_rd = disk_wr = 0.0

    # -- Memory I/O (page faults, page in/out, swap) --
    mem_io = {
        "pgpgin_kbs": 0.0, "pgpgout_kbs": 0.0,
        "pswpin_kbs": 0.0, "pswpout_kbs": 0.0,
        "pgfault_ps": 0.0, "pgmajfault_ps": 0.0,
    }
    if _sys_prev["vmstat"] is not None:
        pv = _sys_prev["vmstat"]
        mem_io["pgpgin_kbs"] = round((vmstat.get("pgpgin", 0) - pv.get("pgpgin", 0)) * 4 / dt / 1024, 1)
        mem_io["pgpgout_kbs"] = round((vmstat.get("pgpgout", 0) - pv.get("pgpgout", 0)) * 4 / dt / 1024, 1)
        mem_io["pswpin_kbs"] = round((vmstat.get("pswpin", 0) - pv.get("pswpin", 0)) * 4 / dt / 1024, 1)
        mem_io["pswpout_kbs"] = round((vmstat.get("pswpout", 0) - pv.get("pswpout", 0)) * 4 / dt / 1024, 1)
        mem_io["pgfault_ps"] = round((vmstat.get("pgfault", 0) - pv.get("pgfault", 0)) / dt, 1)
        mem_io["pgmajfault_ps"] = round((vmstat.get("pgmajfault", 0) - pv.get("pgmajfault", 0)) / dt, 1)

    # -- Memory / Swap --
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    mem_used = mem_total - mem_avail
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = swap_total - swap_free

    # -- Store for next delta --
    _sys_prev["cpu"] = cpu
    _sys_prev["cpu_cores"] = cpu_cores
    _sys_prev["net"] = net
    _sys_prev["disk"] = disk
    _sys_prev["vmstat"] = vmstat
    _sys_prev["psi"] = psi
    _sys_prev["ts"] = now

    return {
        "cpu_pct": cpu_pct,
        "cpu_cores": os.cpu_count() or 1,
        "cpu_per_core": per_core_pct,
        "load1": load[0], "load5": load[1], "load15": load[2],
        "mem_total_kb": mem_total,
        "mem_used_kb": mem_used,
        "mem_pct": round(100.0 * mem_used / mem_total, 1) if mem_total else 0,
        "swap_total_kb": swap_total,
        "swap_used_kb": swap_used,
        "swap_pct": round(100.0 * swap_used / swap_total, 1) if swap_total else 0,
        "net_rx_kbs": net_rx,
        "net_tx_kbs": net_tx,
        "disk_rd_kbs": disk_rd,
        "disk_wr_kbs": disk_wr,
        "mem_io": mem_io,
        "psi": psi,
        "uptime_s": round(_uptime(), 1),
        "procs": _proc_snapshot(),
    }


# ── GPU + per-process utilization (optimization signal) ──────────────
# Answers: "am I at 100%, 80%, or 50%?" so the operator can decide to
# add vLLM parallelism / tensor-parallelism / batch size.
_CLK_TCK = os.sysconf("SC_CLK_TCK")
_proc_prev: dict = {}

# label -> substrings matched against /proc/<pid>/{stat,cmdline} (lowercased)
_PROC_LABELS = {
    "vllm":   ("vllm::worker", "vllm::enginecor", "vllm"),
    "goose":  ("goose",),
    "proxy":  ("proxy/app.py", "app.py"),
    "worker": ("worker/worker.py", "worker.py"),
}


async def sample_gpu() -> dict:
    """Per-GPU utilization, VRAM, temp, power via nvidia-smi (real data)."""
    res = {"available": False, "gpus": [], "util_avg": 0.0, "mem_avg": 0.0}
    rc, out, _ = await _run(
        ["nvidia-smi",
         "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
         "--format=csv,noheader,nounits"], timeout=4)
    if rc != 0 or not out:
        return res
    res["available"] = True
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            g = {"index": int(parts[0]), "util": float(parts[1]),
                 "mem_used": float(parts[2]), "mem_total": float(parts[3]),
                 "temp": float(parts[4]), "power": float(parts[5])}
            g["mem_pct"] = (round(100.0 * g["mem_used"] / g["mem_total"], 1)
                            if g["mem_total"] else 0.0)
            gpus.append(g)
        except Exception:
            continue
    res["gpus"] = gpus
    if gpus:
        res["util_avg"] = round(sum(g["util"] for g in gpus) / len(gpus), 1)
        res["mem_avg"] = round(sum(g["mem_pct"] for g in gpus) / len(gpus), 1)
    return res


def sample_process_util() -> dict:
    """Per-label %CPU from /proc/<pid>/stat tick deltas (goose/proxy/vllm/worker)."""
    now = time.time()
    out: dict = {}
    cur: dict = {}
    cur_pids: dict = {}
    try:
        pids = os.listdir("/proc")
    except Exception:
        return out
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            stat = open("/proc/" + pid + "/stat").read()
            rp = stat.rfind(")")
            fields = stat[rp + 2:].split()
            utime = int(fields[11]); stime = int(fields[12])
            name = stat[stat.index("(") + 1:rp].lower()
            try:
                args = open("/proc/" + pid + "/cmdline").read().lower()
            except Exception:
                args = ""
            ticks = utime + stime
        except Exception:
            continue
        for label, subs in _PROC_LABELS.items():
            if any(s in name or s in args for s in subs):
                cur[label] = cur.get(label, 0) + ticks
                cur_pids.setdefault(label, []).append(int(pid))
                break
    for label in _PROC_LABELS:
        prev = _proc_prev.get(label)
        t = cur.get(label)
        plist = cur_pids.get(label, [])
        if t is None:
            _proc_prev[label] = {"ticks": 0, "ts": now, "pids": []}
            out[label] = {"cpu_pct": 0.0, "pids": []}
            continue
        if prev and prev.get("pids") == plist and prev["ts"] > 0:
            dt = max(0.05, now - prev["ts"])
            cpu_pct = 100.0 * (max(0, t - prev["ticks"]) / _CLK_TCK) / dt
        else:
            cpu_pct = 0.0  # baseline tick (new pids) -> no delta yet
        _proc_prev[label] = {"ticks": t, "ts": now, "pids": plist}
        out[label] = {"cpu_pct": round(min(100.0, cpu_pct), 1), "pids": plist}
    return out


_util_hist: "deque" = deque(maxlen=150)  # ~7.5 min at 3 s poll


def record_util_history(system: dict, gpu: dict, vm: dict) -> None:
    mio = system.get("mem_io", {})
    psi = system.get("psi", {})
    _util_hist.append({
        "t": time.time(),
        "cpu": system.get("cpu_pct", 0),
        "mem": system.get("mem_pct", 0),
        "gpu_util": gpu.get("util_avg", 0),
        "gpu_mem": gpu.get("mem_avg", 0),
        "kv": vm.get("kv_cache_pct"),
        "pgin": mio.get("pgpgin_kbs", 0),
        "pgout": mio.get("pgpgout_kbs", 0),
        "pgfault": mio.get("pgfault_ps", 0),
        "mem_ps": psi.get("memory", {}).get("some", 0),
    })


# ── Database operational metrics ───────────────────────────────────────
async def _db_counts(conn) -> dict:
    """Operational metrics. Time-based stats are split into TODAY (since local
    midnight) and YESTERDAY so the operator sees a real daily cadence, not a
    since-boot counter.  memory_jobs has completed_at (NO updated_at)."""
    # Current-state gauges (live, not time-bounded)
    tasks_total = await conn.fetchval("SELECT count(*) FROM proxy.tasks")
    events_total = await conn.fetchval("SELECT count(*) FROM proxy.events")
    memories_total = await conn.fetchval("SELECT count(*) FROM proxy.memories WHERE active=true")
    knowledge_total = await conn.fetchval("SELECT count(*) FROM proxy.knowledge WHERE active=true")
    jobs_pending = await conn.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE status='pending'")
    jobs_processing = await conn.fetchval("SELECT count(*) FROM proxy.memory_jobs WHERE status='processing'")

    # Today (since local midnight) / yesterday via conditional aggregation.
    t = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE created_at::date = current_date) AS today,"
        " count(*) FILTER (WHERE created_at::date = current_date - 1) AS yest"
        " FROM proxy.tasks")
    e = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE created_at::date = current_date) AS today,"
        " count(*) FILTER (WHERE created_at::date = current_date - 1) AS yest"
        " FROM proxy.events")
    m = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE active=true AND created_at::date = current_date) AS today,"
        " count(*) FILTER (WHERE active=true AND created_at::date = current_date - 1) AS yest"
        " FROM proxy.memories")
    k = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE active=true AND created_at::date = current_date) AS today,"
        " count(*) FILTER (WHERE active=true AND created_at::date = current_date - 1) AS yest"
        " FROM proxy.knowledge")
    j = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE status='done' AND completed_at::date = current_date) AS dt,"
        " count(*) FILTER (WHERE status='done' AND completed_at::date = current_date - 1) AS dy,"
        " count(*) FILTER (WHERE status='failed' AND completed_at::date = current_date) AS ft,"
        " count(*) FILTER (WHERE status='failed' AND completed_at::date = current_date - 1) AS fy"
        " FROM proxy.memory_jobs")
    events_1h = await conn.fetchval(
        "SELECT count(*) FROM proxy.events WHERE created_at > now() - interval '1 hour'")

    return {
        "tasks_total": tasks_total, "events_total": events_total,
        "memories_total": memories_total, "knowledge_total": knowledge_total,
        "jobs_pending": jobs_pending, "jobs_processing": jobs_processing,
        "events_1h": events_1h,
        "tasks_today": t["today"], "tasks_yest": t["yest"],
        "events_today": e["today"], "events_yest": e["yest"],
        "memories_today": m["today"], "memories_yest": m["yest"],
        "knowledge_today": k["today"], "knowledge_yest": k["yest"],
        "jobs_done_today": j["dt"], "jobs_done_yest": j["dy"],
        "jobs_failed_today": j["ft"], "jobs_failed_yest": j["fy"],
    }


async def get_db_metrics() -> dict:
    """Query PG for operational metrics. Reuses the lifespan-managed pool;
    falls back to a one-shot connect only if the pool was never created."""
    try:
        if _pg_pool is not None:
            async with _pg_pool.acquire() as conn:
                return await _db_counts(conn)
        import asyncpg
        conn = await asyncio.wait_for(asyncpg.connect(DB_DSN), timeout=5)
        try:
            return await _db_counts(conn)
        finally:
            await conn.close()
    except Exception as e:
        return {"error": str(e)[:100]}


# ── Process / port helpers (control goes through systemd, never pkill) ─
async def _run(cmd: list, timeout: float = 60.0):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
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
    """Run 'systemctl --user <args>'. All service control goes through here."""
    return await _run(["systemctl", "--user"] + list(args))


async def _port_open(port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=2)
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


async def _unit_main_pid(unit: str) -> int:
    rc, out, _ = await _systemctl("show", "-p", "MainPID", "--value", unit)
    return int(out) if out.isdigit() else 0


async def _unit_active(unit: str) -> str:
    """Return the ActiveState of a user unit (active/inactive/failed/activating)."""
    rc, out, _ = await _systemctl("show", "-p", "ActiveState", "--value", unit)
    return out.strip()


async def _wait_http(url: str, timeout: float = 25.0) -> tuple:
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


async def _wait_lock_fresh(path: str, timeout: float = 25.0) -> tuple:
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

# ── Control actions (all via systemd user units — graceful, no pkill) ──
# Why systemd and not pkill?
#   * pkill -9 (SIGKILL) is uncatchable: the proxy cannot drain in-flight
#     streams, so active LLM responses are cut off mid-token.
#   * systemctl stop sends SIGTERM; the proxy's sigwaitinfo handler logs the
#     sender and drains in-flight requests before exiting (graceful shutdown).
#   * The unit has Restart=always + RestartSec=0.5, so even a hard external
#     kill is recovered automatically and invisibly to Goose.
#   * systemctl only ever touches the unit's own cgroup (the PID it started),
#     never a process matched by name pattern.  Safe by construction.

async def _start(name: str) -> dict:
    """Start a service via its systemd user unit."""
    cfg = SVC_MAP[name]
    unit = cfg["unit"]
    steps = []

    # Already active? Nothing to do.
    state = await _unit_active(unit)
    if state == "active":
        if cfg["kind"] == "http" and await _port_open(cfg["port"]):
            steps.append("already active (port " + str(cfg["port"]) + " open)")
            return {"ok": True, "unit": unit, "steps": steps}
        if cfg["kind"] == "file":
            lp = cfg.get("lock_path", "")
            if lp and os.path.exists(lp):
                ok, detail = await _wait_lock_fresh(lp, timeout=5)
                if ok:
                    steps.append("already active (fresh lock)")
                    return {"ok": True, "unit": unit, "steps": steps}

    # Clear any prior failure so 'start' is not blocked by a failed state.
    await _systemctl("reset-failed", unit)
    rc, out, err = await _systemctl("start", unit)
    steps.append("systemctl start rc=" + str(rc) + " " + (out or err).strip()[:120])
    if rc != 0:
        return {"ok": False, "unit": unit, "error": "start failed: " + err, "steps": steps}

    # Verify it came up.
    if cfg["kind"] == "http":
        ok, detail = await _wait_http(cfg["health_url"])
    else:
        ok, detail = await _wait_lock_fresh(cfg["lock_path"])
    steps.append("verify: " + detail)
    return {"ok": ok, "unit": unit, "steps": steps}


async def _stop(name: str) -> dict:
    """Stop a service via its systemd user unit. Sends SIGTERM (graceful:
    the proxy drains in-flight streams before exiting). Never SIGKILL."""
    cfg = SVC_MAP[name]
    unit = cfg["unit"]
    steps = []

    rc, out, err = await _systemctl("stop", unit)
    steps.append("systemctl stop rc=" + str(rc) + " " + (out or err).strip()[:120])

    # Confirm the port is actually free (it should be once SIGTERM is handled).
    if cfg["kind"] == "http":
        # Give the graceful drain a moment, then report the port state.
        await asyncio.sleep(1.0)
        free = not await _port_open(cfg["port"])
        steps.append("port " + str(cfg["port"]) + ": " + ("free" if free else "still held"))
    else:
        lp = cfg.get("lock_path", "")
        if lp and os.path.exists(lp):
            try:
                os.remove(lp)
                steps.append("removed lock file")
            except FileNotFoundError:
                steps.append("no lock file")

    return {"ok": rc == 0, "unit": unit, "steps": steps}


async def _restart(name: str) -> dict:
    """Graceful restart: systemctl restart = stop (SIGTERM, drain) then start.
    In-flight streams finish before the new instance binds the port."""
    cfg = SVC_MAP[name]
    unit = cfg["unit"]
    steps = []

    await _systemctl("reset-failed", unit)
    rc, out, err = await _systemctl("restart", unit)
    steps.append("systemctl restart rc=" + str(rc) + " " + (out or err).strip()[:120])
    if rc != 0:
        return {"ok": False, "unit": unit, "error": "restart failed: " + err, "steps": steps}

    if cfg["kind"] == "http":
        ok, detail = await _wait_http(cfg["health_url"])
    else:
        ok, detail = await _wait_lock_fresh(cfg["lock_path"])
    steps.append("verify: " + detail)
    return {"ok": ok, "unit": unit, "steps": steps}


# ── Background poller ──────────────────────────────────────────────────
async def poll_health():
    _maybe_reload_config()
    global health_state
    while not _shutdown_event.is_set():
        t0 = time.time()
        services = {}
        for name, cfg in SERVICES.items():
            if cfg["type"] == "tcp":
                services[name] = await check_tcp(cfg["host"], cfg["port"])
            elif cfg["type"] == "http":
                services[name] = await check_http(cfg["url"])
            elif cfg["type"] == "pg":
                services[name] = await check_pg()
            elif cfg["type"] == "file":
                services[name] = await check_worker_file(cfg["path"])

        db_metrics = await get_db_metrics()
        system = sample_system()
        vllm_metrics = await check_vllm_metrics(_vllm_metrics_url())
        gpu = await sample_gpu()
        process_util = sample_process_util()
        record_util_history(system, gpu, vllm_metrics)

        health_state = {
            "services": services,
            "db_metrics": db_metrics,
            "system": system,
            "vllm_metrics": vllm_metrics,
            "gpu": gpu,
            "process_util": process_util,
            "util_history": list(_util_hist),
            "last_update": time.time(),
        }
        # Sleep the remainder of POLL_INTERVAL so the poll period stays stable
        # even if a check is slow.
        elapsed = time.time() - t0
        await asyncio.sleep(max(0.5, POLL_INTERVAL - elapsed))


# ── Routes ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "up", "port": PORT, "pid": os.getpid()}


@app.get("/api/health")
async def api_health():
    return JSONResponse(health_state)


@app.post("/api/control/{name}/{action}")
async def control(name: str, action: str, request: Request):
    if not _auth_ok(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if name not in SVC_MAP:
        return JSONResponse({"ok": False, "error": "unknown service: " + name}, status_code=400)
    if action == "start":
        return JSONResponse(await _start(name))
    if action == "stop":
        return JSONResponse(await _stop(name))
    if action == "restart":
        return JSONResponse(await _restart(name))
    return JSONResponse({"ok": False, "error": "unknown action: " + action}, status_code=400)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


# ── /deliverable: agent-generated document registry ────────────────────
# The AGENT registers each deliverable via POST /api/deliverable at task
# completion (idempotent upsert — no polling, no file scanning).
# GET /deliverable renders the table; GET /deliverable/file serves the doc.
_DELIVERABLE_ROOTS = [os.environ.get("CTXGATE_DELIVERABLE_ROOT",
                                     os.path.expanduser("~"))]


def _deliverable_file_ok(path: str) -> bool:
    if not path:
        return False
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    return any(rp == r or rp.startswith(r.rstrip("/") + "/")
               for r in _DELIVERABLE_ROOTS)


def _deliverable_html(rows) -> str:
    import html as _html
    trs = []
    for r in rows:
        d = dict(r)
        path = d.get("document_path", "")
        if path:
            link = ('<a href="/deliverable/file?path='
                    + _html.escape(path, quote=True)
                    + '" target="_blank" rel="noopener">'
                    + _html.escape(os.path.basename(path)) + '</a>')
        else:
            link = '—'
        created = d.get("created_at")
        created_s = created.strftime("%Y-%m-%d %H:%M") if created else ""
        trs.append(
            '<tr>'
            + '<td>' + _html.escape(str(d.get("id", ""))[:8]) + '</td>'
            + '<td>' + _html.escape(d.get("name", "")) + '</td>'
            + '<td>' + _html.escape(d.get("session_type", "")) + '</td>'
            + '<td>' + _html.escape(d.get("working_dir", "")) + '</td>'
            + '<td>' + _html.escape(d.get("provider_name", "")) + '</td>'
            + '<td>' + _html.escape(d.get("summary", "")) + '</td>'
            + '<td>' + _html.escape(created_s) + '</td>'
            + '<td>' + link + '</td>'
            + '</tr>'
        )
    body = '\n'.join(trs) if trs else '<tr><td colspan="8">No deliverables yet.</td></tr>'
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<title>Deliverables</title>'
            "<style>body{font-family:system-ui,sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}"
            "h1{font-size:20px}table{border-collapse:collapse;width:100%;font-size:13px}"
            "th,td{border:1px solid #2a2f3a;padding:6px 8px;text-align:left;vertical-align:top}"
            "th{background:#1a1f2b;position:sticky;top:0}tr:nth-child(even){background:#141821}"
            "a{color:#5ab0ff}td:first-child{font-family:monospace;color:#888}</style></head>"
            "<body><h1>Agent Deliverables</h1>"
            "<table><thead><tr><th>id</th><th>name</th><th>session</th>"
            "<th>working_dir</th><th>provider</th><th>summary</th>"
            "<th>created</th><th>document</th></tr></thead><tbody>"
            + body + "</tbody></table></body></html>")


@app.get("/deliverable", response_class=HTMLResponse)
async def deliverable_dashboard():
    rows = []
    if _pg_pool is not None:
        try:
            async with _pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, name, session_type, working_dir, provider_name, summary, "
                    "document_path, created_at, updated_at FROM proxy.deliverables "
                    "ORDER BY created_at DESC LIMIT 500")
        except Exception as e:
            _log.warning("deliverable query failed: %s", e)
    return _deliverable_html(rows)


@app.get("/api/deliverable")
async def deliverable_api():
    if _pg_pool is None:
        return JSONResponse({"ok": False, "error": "no db"}, status_code=503)
    try:
        async with _pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, session_type, working_dir, provider_name, summary, "
                "document_path, created_at, updated_at FROM proxy.deliverables "
                "ORDER BY created_at DESC LIMIT 500")
        out = []
        for r in rows:
            d = dict(r)
            if d.get("id") is not None:
                d["id"] = str(d["id"])
            for k in ("created_at", "updated_at"):
                if d.get(k):
                    d[k] = d[k].isoformat()
            out.append(d)
        return JSONResponse({"ok": True, "count": len(out), "rows": out})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/deliverable")
async def deliverable_create(request: Request):
    if not _auth_ok(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if _pg_pool is None:
        return JSONResponse({"ok": False, "error": "no db"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    name = (body.get("name") or "").strip()
    doc = (body.get("document_path") or "").strip()
    if not name or not doc:
        return JSONResponse({"ok": False, "error": "name and document_path required"}, status_code=400)
    if not _deliverable_file_ok(doc):
        return JSONResponse({"ok": False, "error": "document_path outside allowed roots"}, status_code=400)
    try:
        async with _pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO proxy.deliverables
                    (name, session_type, working_dir, provider_name, summary, document_path)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (working_dir, document_path) DO UPDATE SET
                    name = EXCLUDED.name,
                    session_type = EXCLUDED.session_type,
                    provider_name = EXCLUDED.provider_name,
                    summary = EXCLUDED.summary,
                    updated_at = now()
                RETURNING id
                """,
                name,
                (body.get("session_type") or "goose").strip(),
                (body.get("working_dir") or "").strip(),
                (body.get("provider_name") or "").strip(),
                (body.get("summary") or "").strip(),
                doc,
            )
            return JSONResponse({"ok": True, "id": str(row["id"])})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/deliverable/file")
async def deliverable_file(request: Request):
    path = request.query_params.get("path", "")
    if not _deliverable_file_ok(path):
        return JSONResponse({"ok": False, "error": "forbidden path"}, status_code=403)
    if not os.path.isfile(path):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    from fastapi.responses import FileResponse
    return FileResponse(path, filename=os.path.basename(path))


# ── Config (config.yaml) editor ────────────────────────────────────────
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
    global _last_config_write
    if not _auth_ok(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if time.time() - _last_config_write < 5.0:
        return JSONResponse({"ok": False, "error": "rate limited (min 5s between writes)"}, status_code=429)
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
    try:
        pi = float(_get(cfg, "dashboard", "poll_interval", default=3.0) or 3.0)
        wt = float(_get(cfg, "dashboard", "watchdog_timeout", default=90) or 90)
        if pi >= wt:
            return JSONResponse({"ok": False,
                "error": "poll_interval (" + str(pi) + "s) must be < watchdog_timeout (" + str(wt) + "s)"},
                status_code=400)
    except Exception:
        pass
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "write failed: " + str(e)}, status_code=400)
    _last_config_write = time.time()
    _touch_config_mtime()
    _maybe_reload_config()
    return JSONResponse({"ok": True, "path": CONFIG_PATH})


# ── Runtime env file editor ────────────────────────────────────────────
@app.get("/api/env")
async def get_env():
    """Return the runtime env file (proxy + worker)."""
    try:
        with open(ENV_PATH) as f:
            text = f.read()
        return JSONResponse({"ok": True, "path": ENV_PATH, "text": text})
    except FileNotFoundError:
        return JSONResponse({"ok": True, "path": ENV_PATH, "text": ""})
    except Exception as e:
        return JSONResponse({"ok": False, "error": "read failed: " + str(e)}, status_code=400)


@app.post("/api/env")
async def put_env(request: Request):
    """Write the runtime env file. Atomic write, mode 600."""
    global _last_config_write
    if not _auth_ok(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if time.time() - _last_config_write < 5.0:
        return JSONResponse({"ok": False, "error": "rate limited (min 5s between writes)"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    text = body.get("text")
    if not isinstance(text, str):
        return JSONResponse({"ok": False, "error": "body must be {text: <string>}"}, status_code=400)
    ok, err = _validate_env_text(text)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        tmp = ENV_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_PATH)
        try:
            os.chmod(ENV_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        return JSONResponse({"ok": False, "error": "write failed: " + str(e)}, status_code=400)
    _last_config_write = time.time()
    return JSONResponse({"ok": True, "path": ENV_PATH})


@app.post("/api/env/save-and-restart")
async def put_env_and_restart(request: Request):
    """Write env then gracefully restart proxy + worker via systemd."""
    r = await put_env(request)
    if r.status_code != 200:
        return r
    results = {}
    for name in ("ctxgate_proxy", "worker"):
        try:
            results[name] = await _restart(name)
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}
    return JSONResponse({"ok": True, "path": ENV_PATH, "restart": results})


# ── Log explorer ───────────────────────────────────────────────────────
@app.get("/api/log")
async def get_log(request: Request):
    p = request.query_params
    n = int(p.get("lines", "400"))
    n = max(1, min(n, 3000))
    source = p.get("source", "dashboard")
    if source == "journal":
        # Correct unit name: ctxgate-dashboard.service (was ctxproxy-* before).
        try:
            res = await _run(["journalctl", "--user", "-u", "ctxgate-dashboard.service",
                              "-n", str(n), "--no-pager"])
            text = res[1] if isinstance(res, (list, tuple)) else str(res)
        except Exception as e:
            return JSONResponse({"ok": False, "source": source, "error": str(e)}, status_code=400)
    else:
        path = os.environ.get("CTXGATE_LOG", os.path.join(_ROOT, "dashboard.log"))
        try:
            with open(path) as f:
                lines = f.readlines()
            text = "".join(lines[-n:])
        except Exception as e:
            return JSONResponse({"ok": False, "source": source, "error": "read failed: " + str(e)}, status_code=400)
    return JSONResponse({"ok": True, "source": source, "text": text})

# ── Dashboard HTML (Control Room) ──────────────────────────────────────
# Modern, information-dense layout:
#   Row 1: 5 service boxes (PG / Proxy / vLLM / LM / Worker) with LEDs
#   Row 2: a SYSTEM panel (htop-style: CPU, MEM, SWAP, LOAD, NET, DISK,
#          uptime) + a TOP PROCESSES table (top-12 by RSS)
#   Bottom bar: meaningful, labelled counters (no more mystery numbers)
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><polygon points='16,2 28,9 28,23 16,30 4,23 4,9' fill='none' stroke='%2300ff88' stroke-width='2.5'/><path d='M18 8L12 18h4l-2 6 6-10h-4z' fill='%2300e5ff'/></svg>">
<title>CTXGATE</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
--bg:#0a0e14;--panel:#0d1520;--panel2:#101a26;--border:#1e3a5f;
--green:#00ff88;--yellow:#ffaa00;--red:#ff3344;--blue:#4488ff;
--cyan:#22d3ee;--text:#c8d8e8;--dim:#4a6a8a;
}
body{font-family:'Consolas','SF Mono','Menlo','Courier New',monospace;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}
/* Header */
.header{background:#080c12;border-bottom:2px solid var(--border);padding:6px 20px;display:flex;justify-content:space-between;align-items:center;height:46px;flex-shrink:0;gap:12px}
.header .left{display:flex;align-items:center;gap:10px}
.header h1{font-size:1em;color:var(--blue);letter-spacing:3px;text-transform:uppercase;white-space:nowrap}
.header .clock{font-size:.85em;color:var(--dim);white-space:nowrap}
.btn{background:var(--panel);border:1px solid var(--border);color:var(--blue);font-family:inherit;padding:5px 12px;cursor:pointer;font-size:.8em;letter-spacing:1px;flex-shrink:0}
.btn:hover{border-color:var(--green);color:var(--green)}
.btn.green{color:var(--green)}
/* Main grid: top row = 5 service boxes, bottom row = system + processes */
.main{flex:1;display:grid;grid-template-columns:repeat(5,1fr) 1.6fr;grid-template-rows:auto 1fr;gap:8px;padding:10px;min-height:0}
/* Service boxes (top row) */
.box{background:var(--panel);border:2px solid var(--border);border-radius:6px;padding:10px;position:relative;display:flex;flex-direction:column;transition:border-color .3s,box-shadow .3s;min-width:0}
.box.up{border-color:var(--green);box-shadow:0 0 14px #00ff8833}
.box.degraded{border-color:var(--yellow);box-shadow:0 0 14px #ffaa0033}
.box.down{border-color:var(--red);box-shadow:0 0 14px #ff334433}
.box .title{font-size:.72em;font-weight:bold;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;display:flex;align-items:center;gap:6px}
.box .led{width:9px;height:9px;border-radius:50%;background:#333;flex-shrink:0}
.box.up .led{background:var(--green);box-shadow:0 0 9px var(--green);animation:pulse 2s infinite}
.box.degraded .led{background:var(--yellow);box-shadow:0 0 9px var(--yellow)}
.box.down .led{background:var(--red);box-shadow:0 0 9px var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.box .metrics{display:flex;flex-direction:column;gap:3px;font-size:.76em}
.box .metric-row{display:flex;justify-content:space-between;gap:6px;padding:1px 0}
.box .metric-row .k{color:var(--dim)}
.box .metric-row .v{color:var(--text);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.box .metric-row .v.big{color:var(--green);font-size:1.3em;font-weight:bold}
.box .metric-row .v.blue{color:var(--cyan)}
.box .note{font-size:.68em;color:var(--dim);margin-top:4px;line-height:1.3;min-height:0}
.controls{display:flex;gap:3px;margin-top:8px;padding-top:6px;border-top:1px solid #111}
.controls button{flex:1;font-family:inherit;font-size:.6em;font-weight:bold;padding:3px 4px;border-radius:3px;cursor:pointer;text-transform:uppercase;letter-spacing:.5px;border:1px solid;transition:all .2s;min-width:0}
.controls button.s{background:#0a2a1a;color:var(--green);border-color:#1a5a3a}
.controls button.p{background:#2a1a0a;color:var(--yellow);border-color:#5a4a1a}
.controls button.r{background:#2a0a0a;color:var(--red);border-color:#5a1a1a}
.controls button:disabled{opacity:.4;cursor:wait}
/* System panel (bottom-left area) */
.syspanel{background:var(--panel);border:2px solid var(--border);border-radius:6px;padding:10px;display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;align-content:start}
.syspanel .sp-title{grid-column:1/3;font-size:.72em;font-weight:bold;text-transform:uppercase;letter-spacing:1.5px;color:var(--cyan);margin-bottom:2px}
.gauge{display:flex;flex-direction:column;gap:3px}
.gauge .gl{display:flex;justify-content:space-between;font-size:.7em}
.gauge .gl .k{color:var(--dim)}
.gauge .gl .v{color:var(--text)}
.bar-track{height:7px;background:#05080d;border:1px solid var(--border);border-radius:3px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .5s ease}
.fill-cpu{background:linear-gradient(90deg,var(--cyan),var(--blue))}
.fill-mem{background:linear-gradient(90deg,var(--green),#00cc77)}
.fill-swap{background:linear-gradient(90deg,var(--yellow),#ffcc44)}
.fill-net{background:linear-gradient(90deg,var(--blue),var(--cyan))}
.fill-disk{background:linear-gradient(90deg,#c084fc,#a855f7)}
.fill-gpu{background:linear-gradient(90deg,var(--red),#ff6b6b)}
.fill-vram{background:linear-gradient(90deg,#ec4899,#f472b6)}
.utilpanel{grid-column:2/-1;grid-row:2;background:var(--panel);border:2px solid var(--border);border-radius:6px;padding:10px;display:flex;flex-direction:column;gap:8px;min-height:0;overflow:hidden}
.utilpanel .sp-title{font-size:.72em;font-weight:bold;text-transform:uppercase;letter-spacing:1.5px;color:var(--cyan)}
.util-hint{color:var(--dim);font-weight:normal;text-transform:none;letter-spacing:0;margin-left:8px}
.util-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.util-grid-6{grid-template-columns:repeat(6,1fr)}
.corebars{display:flex;gap:2px;margin-top:2px}
.corebar{flex:1;height:4px;background:#05080d;border-radius:1px;overflow:hidden}
.corebar-fill{height:100%;border-radius:1px;transition:width .5s}
.fill-mio{background:linear-gradient(90deg,#38bdf8,#06b6d4)}
.fill-pf{background:linear-gradient(90deg,#fbbf24,#f59e0b)}
.ucard{background:#0a0e14;border:1px solid var(--border);border-radius:5px;padding:8px;display:flex;flex-direction:column;gap:5px;transition:border-color .3s}
.uc-head{display:flex;align-items:center;gap:6px}
.uc-ic{font-size:1.1em}
.uc-name{font-size:.72em;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.uc-val{margin-left:auto;font-size:1.2em;font-weight:bold;color:var(--text)}
.spark{height:28px;color:var(--cyan);display:flex;align-items:flex-end}
.util-proc{display:flex;flex-direction:column;gap:5px}
.uc-head.small{font-size:.72em}
.procbars{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 16px}
.pbar{display:flex;align-items:center;gap:8px}
.pbar-name{width:54px;font-size:.72em;color:var(--dim);text-transform:uppercase}
.pbar .bar-track{flex:1}
.pbar-val{width:44px;text-align:right;font-size:.72em;color:var(--text)}
.syspanel .corebars{grid-column:1/3}
.fill-kv{background:linear-gradient(90deg,#f472b6,#ec4899)}
.fill-pfx{background:linear-gradient(90deg,#34d399,#10b981)}
.kvbar{display:flex;flex-direction:column;gap:3px;margin-top:3px}
.kvbar-l{display:flex;justify-content:space-between;font-size:.76em}
.kvbar-l .k{color:var(--dim)}
.kvbar-l .v{color:var(--text)}
/* Top processes table (bottom-right) */
.procs{background:var(--panel);border:2px solid var(--border);border-radius:6px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.procs .pt{font-size:.72em;font-weight:bold;text-transform:uppercase;letter-spacing:1.5px;color:var(--cyan);padding:2px 0 6px}
.procs .table-wrap{flex:1;overflow-y:auto;min-height:0}
.ptable{width:100%;border-collapse:collapse;font-size:.72em}
.ptable th{text-align:left;padding:4px 6px;border-bottom:2px solid var(--border);color:var(--dim);font-size:.72em;letter-spacing:.5px;text-transform:uppercase;position:sticky;top:0;background:var(--panel)}
.ptable td{padding:3px 6px;border-bottom:1px solid #111;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ptable tr:hover td{background:rgba(255,255,255,.03)}
.ptable .num{text-align:right;color:var(--cyan)}
.ptable .pname{max-width:120px}
/* Bottom bar */
.bar{background:#080c12;border-top:2px solid var(--border);padding:8px 18px;display:flex;gap:22px;align-items:center;height:48px;flex-shrink:0;font-size:.8em;overflow-x:auto}
.bar .m{display:flex;align-items:center;gap:6px;white-space:nowrap}
.bar .m .l{color:var(--dim);text-transform:uppercase;font-size:.66em;letter-spacing:.5px}
.bar .m .n{color:var(--green);font-size:1.15em;font-weight:bold}
.bar .m .n.blue{color:var(--cyan)}
.bar .m .n.err{color:var(--red)}
.bar .m .n.small{font-size:.85em}
/* ── Deep-explanation tooltips (no space saved: wide, rich) ── */
[data-tip]{cursor:help}
#tt{position:fixed;z-index:9999;max-width:440px;background:#0c1420;border:1px solid var(--cyan);border-radius:8px;padding:11px 13px;box-shadow:0 12px 34px rgba(0,0,0,.6),0 0 0 1px rgba(34,211,238,.18);display:none;pointer-events:none;font-size:.82em;line-height:1.5;color:var(--text)}
#tt .th{color:var(--cyan);font-weight:700;font-size:.92em;text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
#tt .tb{color:var(--text)}
#tt .tw{margin-top:7px;color:var(--dim);font-size:.9em;font-style:italic}
/* Modals */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:100;justify-content:center;align-items:center}
.modal.open{display:flex}
.mbox{width:820px;max-width:95vw;max-height:92vh;background:var(--bg);border:2px solid var(--border);display:flex;flex-direction:column}
.m-head{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel)}
.m-head h2{font-size:.95em;color:var(--blue);letter-spacing:3px}
.m-head .close{cursor:pointer;color:var(--red);font-size:1.2em}
.m-hint{padding:6px 16px;font-size:.72em;color:var(--dim);border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center}
.m-text{flex:1;min-height:380px;background:#05080d;color:var(--text);border:none;outline:none;padding:14px;font-family:inherit;font-size:.85em;resize:none;line-height:1.45;overflow:auto}
.m-foot{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-top:1px solid var(--border);background:var(--panel)}
.m-foot .msg{font-size:.8em;color:var(--dim)}
.m-btn{background:var(--panel);border:1px solid var(--border);color:var(--text);font-family:inherit;padding:6px 16px;cursor:pointer;font-size:.85em;letter-spacing:1px;margin-left:8px}
.m-btn:hover{border-color:var(--blue)}
.m-btn.save{border-color:var(--green);color:var(--green)}
.m-btn.restart{border-color:var(--yellow);color:var(--yellow)}
.m-tab{background:transparent;border:1px solid var(--border);color:var(--text);font-family:inherit;padding:4px 10px;cursor:pointer;font-size:.78em;letter-spacing:.5px;margin-right:4px}
.m-tab.active{border-color:var(--green);color:var(--green)}
/* Log modal */
#logModal .mbox{width:980px}
.lg-ctrl{display:flex;gap:16px;align-items:center;padding:7px 16px;border-bottom:1px solid var(--border);font-size:.78em;color:var(--dim)}
.lg-ctrl label{cursor:pointer;user-select:none;display:flex;gap:5px;align-items:center}
.lg-ctrl input[type=checkbox]{accent-color:var(--green)}
.lg-ctrl select{background:var(--panel);color:var(--text);border:1px solid var(--border);font-family:inherit;font-size:.9em;padding:2px 6px}
#lgStat{margin-left:auto;color:var(--dim)}
#logText{flex:1;overflow:auto;background:#05080d;color:var(--text);padding:12px 16px;font-family:inherit;font-size:.78em;line-height:1.4;white-space:pre-wrap;word-break:break-word;margin:0}
/* Settings table */
.sb-table-wrap{overflow-y:auto;max-height:60vh;padding:10px 16px}
.sb-table{width:100%;border-collapse:collapse;font-size:.82em}
.sb-table th{text-align:left;padding:6px 8px;border-bottom:2px solid var(--border);color:var(--dim);font-size:.76em;letter-spacing:.5px;text-transform:uppercase}
.sb-table td{padding:5px 8px;border-bottom:1px solid var(--border)}
.sb-table .var-name{font-family:monospace;color:var(--text);font-size:.9em}
.sb-table .type-badge{font-size:.66em;padding:2px 6px;border-radius:3px;letter-spacing:.5px}
.type-int{background:rgba(99,102,241,.15);color:#818cf8}
.type-float{background:rgba(234,179,8,.15);color:#facc15}
.type-str{background:rgba(16,185,129,.15);color:#34d399}
.type-url{background:rgba(59,130,246,.15);color:#60a5fa}
.type-bool{background:rgba(239,68,68,.15);color:#f87171}
.sb-table input[type="text"],.sb-table input[type="number"]{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:4px 8px;font-size:.9em;font-family:monospace}
.sb-table input:focus{border-color:var(--blue);outline:none}
.sb-table .src-badge{font-size:.66em;padding:2px 5px;border-radius:3px}
.src-default{color:var(--dim)}
.src-custom{color:var(--green)}
.unit-label{font-size:.72em;color:var(--dim);font-style:italic}
.unit-label.dim{opacity:.4}
</style>
</head>
<body>
<div class="header">
  <div class="left">
    <button class="btn" onclick="openSettings()">&#9881; SETTINGS</button>
    <button class="btn green" onclick="openLog()">&#128203; LOGS</button>
    <h1>&#9889; CTXGATE</h1>
  </div>
  <div class="clock" id="clock">--:--:--</div>
</div>

<div class="main">
  <!-- Row 1: service boxes -->
  <div class="box" id="box-pg">
    <div class="title"><span class="led"></span>POSTGRESQL</div>
    <div class="metrics">
      <div class="metric-row"><span class="k">Status</span><span class="v" id="pg-status">--</span></div>
      <div class="metric-row"><span class="k">Tasks</span><span class="v" id="pg-tasks">--</span></div>
      <div class="metric-row"><span class="k">Events (1h)</span><span class="v" id="pg-ev1h">--</span></div>
      <div class="metric-row"><span class="k">Memories</span><span class="v" id="pg-mem">--</span></div>
      <div class="metric-row"><span class="k">Knowledge</span><span class="v" id="pg-know">--</span></div>
    </div>
    <div class="note">Session store + durable memory. Events(1h) = activity in the last hour.</div>
  </div>

  <div class="box" id="box-proxy">
    <div class="title"><span class="led"></span>CTXGATE PROXY</div>
    <div class="metrics">
      <div class="metric-row"><span class="k">Status</span><span class="v" id="px-status">--</span></div>
      <div class="metric-row"><span class="k">Latency</span><span class="v big" id="px-lat">--</span></div>
      <div class="metric-row"><span class="k">Unit</span><span class="v blue" id="px-unit">--</span></div>
    </div>
    <div class="controls">
      <button class="s" onclick="ctrl(event,'ctxgate_proxy','start')">START</button>
      <button class="p" onclick="ctrl(event,'ctxgate_proxy','stop')">STOP</button>
      <button class="r" onclick="ctrl(event,'ctxgate_proxy','restart')">RESTART</button>
    </div>
    <div class="note">:9201 Context window manager. STOP/RESTART are graceful (SIGTERM → drains in-flight).</div>
  </div>

  <div class="box" id="box-vllm">
    <div class="title"><span class="led"></span>VLLM 27B</div>
    <div class="metrics">
      <div class="metric-row"><span class="k">Status</span><span class="v" id="vl-status">--</span></div>
      <div class="metric-row"><span class="k">Latency</span><span class="v" id="vl-lat">--</span></div>
      <div class="metric-row"><span class="k">Prefill 5m</span><span class="v big" id="vl-prefill">--</span></div>
      <div class="metric-row"><span class="k">Gen real</span><span class="v big" id="vl-genreal">--</span></div>
      <div class="metric-row"><span class="k">Gen total</span><span class="v" id="vl-gentotal">--</span></div>
      <div class="metric-row"><span class="k">Reqs run/w</span><span class="v" id="vl-reqs">--</span></div>
      <div class="kvbar"><div class="kvbar-l"><span class="k">KV cache</span><span class="v" id="vl-kv">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-kv" id="b-kv" style="width:0%"></div></div></div>
      <div class="kvbar"><div class="kvbar-l"><span class="k">Pfx cache hit</span><span class="v" id="vl-pfx">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-pfx" id="b-pfx" style="width:0%"></div></div></div>
    </div>
    <div class="note">:29000 Qwen3.8-27B. 5m rolling rates from /metrics (real data).</div>
  </div>

  <div class="box" id="box-lm">
    <div class="title"><span class="led"></span>LM STUDIO 4B</div>
    <div class="metrics">
      <div class="metric-row"><span class="k">Status</span><span class="v" id="lm-status">--</span></div>
      <div class="metric-row" data-tip="Latency|Wall-clock time of the most recent LM Studio API call (connect + inference + response)."><span class="k">Latency</span><span class="v big" id="lm-lat">--</span></div>
      <div class="metric-row" data-tip="Context|Average prompt token count per request (left) and max (right). Shows how much of the 32K context window is being used."><span class="k">Context</span><span class="v" id="lm-ctx">--</span></div>
      <div class="metric-row" data-tip="Throughput|Requests per minute (left) and total tokens generated (right). RPM from the last 5 min window; tokens = cumulative output."><span class="k">Throughput</span><span class="v" id="lm-req">--</span></div>
      <div class="metric-row"><span class="k">Engine</span><span class="v">Mistral API</span></div>
    </div>
    <div class="note">Small model for the memory-extraction worker. Context/throughput from the worker status file.</div>
  </div>

  <div class="box" id="box-worker">
    <div class="title" data-tip="WORKER 4B|The memory-extraction daemon (qwen3-4b-instruct). It consumes context-gate events and distils them into durable memories & knowledge stored in PostgreSQL. It writes a lock file with its PID and a heartbeat every few seconds."><span class="led"></span>WORKER 4B</div>
    <div class="metrics">
      <div class="metric-row" data-tip="Worker Status|up = heartbeat younger than 30s. degraded = 30-120s (may be stuck). down = no lock file or heartbeat older than 120s (frozen/dead)."><span class="k">Status</span><span class="v" id="wk-status">--</span></div>
      <div class="metric-row" data-tip="Worker PID|The operating-system process ID of the running worker, read from its lock file."><span class="k">PID</span><span class="v" id="wk-pid">--</span></div>
      <div class="metric-row" data-tip="Worker Heartbeat|Seconds since the worker last wrote its heartbeat. A healthy worker stays under 30s. If this climbs, the worker is blocked or dead."><span class="k">Heartbeat</span><span class="v big" id="wk-hb">--</span></div>
      <div class="metric-row" data-tip="Jobs Processing|Number of memory jobs currently being processed by worker consumers. Non-zero = workers are actively extracting memories. With 10 consumers you can see up to 10 in parallel."><span class="k">Processing</span><span class="v big" id="wk-proc" style="color:var(--blue)">0</span></div>
      <div class="metric-row" data-tip="Jobs Pending|Memory jobs queued and waiting for a free consumer. High number = consumers are saturated or stuck."><span class="k">Pending</span><span class="v" id="wk-pend">0</span></div>
      <div class="metric-row" data-tip="Done Today|Memory jobs completed since local midnight. The throughput signal."><span class="k">Done (today)</span><span class="v" id="wk-done">0</span></div>
    </div>
    <div class="controls">
      <button class="s" onclick="ctrl(event,'worker','start')">START</button>
      <button class="p" onclick="ctrl(event,'worker','stop')">STOP</button>
      <button class="r" onclick="ctrl(event,'worker','restart')">RESTART</button>
    </div>
    <div class="note">Memory-extraction daemon (heartbeats to a lock file).</div>
  </div>

  <!-- Row 2: system panel + top processes -->
  <div class="syspanel" id="syspanel">
    <div class="sp-title">&#128450; SYSTEM</div>
    <div class="gauge">
      <div class="gl"><span class="k">CPU</span><span class="v" id="s-cpu">--</span></div>
      <div class="bar-track"><div class="bar-fill fill-cpu" id="b-cpu" style="width:0%"></div></div>
      <div class="corebars" id="corebars"></div>
    </div>
    <div class="gauge">
      <div class="gl"><span class="k">MEMORY</span><span class="v" id="s-mem">--</span></div>
      <div class="bar-track"><div class="bar-fill fill-mem" id="b-mem" style="width:0%"></div></div>
      <div class="gl"><span class="k">Page I/O</span><span class="v" id="s-pg">--</span></div>
      <div class="gl"><span class="k">Page faults</span><span class="v" id="s-pf">--</span></div>
    </div>
    <div class="gauge">
      <div class="gl"><span class="k">SWAP</span><span class="v" id="s-swap">--</span></div>
      <div class="bar-track"><div class="bar-fill fill-swap" id="b-swap" style="width:0%"></div></div>
      <div class="gl"><span class="k">Memory PSI</span><span class="v" id="s-psi">--</span></div>
    </div>
    <div class="gauge">
      <div class="gl"><span class="k">LOAD (1/5/15)</span><span class="v" id="s-load">--</span></div>
      <div class="gl"><span class="k">UPTIME</span><span class="v" id="s-uptime">--</span></div>
    </div>
    <div class="gauge">
      <div class="gl"><span class="k">NET RX / TX</span><span class="v" id="s-net">--</span></div>
      <div class="bar-track"><div class="bar-fill fill-net" id="b-net" style="width:0%"></div></div>
      <div class="gl"><span class="k">IO PSI</span><span class="v" id="s-psi-io">--</span></div>
    </div>
    <div class="gauge">
      <div class="gl"><span class="k">DISK RD / WR</span><span class="v" id="s-disk">--</span></div>
      <div class="bar-track"><div class="bar-fill fill-disk" id="b-disk" style="width:0%"></div></div>
      <div class="gl"><span class="k">CPU PSI</span><span class="v" id="s-psi-cpu">--</span></div>
    </div>
  </div>

  <div class="procs">
    <div class="pt" data-tip="Top Processes (by memory)|The 12 heaviest processes by resident memory (RSS). CPU% is a real 3-second tick delta. The CPU column is PER-CORE: a process pinned to 4 cores shows ~total&divide;4, so '0-3 (95%)' means those 4 cores are each ~95% busy - the signal for pinning / parallelism decisions.">&#128202; TOP PROCESSES (by memory)</div>
    <div class="table-wrap">
      <table class="ptable">
        <thead><tr><th data-tip="PID|Process ID.">PID</th><th data-tip="Name|Process name from /proc/PID/comm (truncated to 20 chars).">NAME</th><th data-tip="Memory|Resident set size (physical RAM currently in use).">MEM</th><th data-tip="Memory %|RSS &divide; 60 GB total, i.e. the share of host RAM.">MEM%</th><th data-tip="CPU (per-core)|Total CPU% over the 3s window, shown as 'allowed-cores (per-core%)'. e.g. 0-3 (95%) = 4 pinned cores each ~95%. All 32 cores = a single number.">CPU(s)</th></tr></thead>
        <tbody id="procBody"><tr><td colspan="5" style="color:var(--dim)">loading…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="utilpanel" id="utilpanel">
    <div class="sp-title">&#9889; UTILIZATION <span class="util-hint">100% = saturated &rarr; optimize (e.g. vLLM parallel=2)</span></div>
    <div class="util-grid util-grid-6">
      <div class="ucard" id="uc-cpu">
        <div class="uc-head" data-tip="CPU|Host CPU usage across all 32 cores, from /proc/stat tick deltas. Red at &ge;90%, yellow at &ge;70%. The sparkline is the last ~7.5 min."><span class="uc-ic">&#9881;</span><span class="uc-name">CPU</span><span class="uc-val" id="u-cpu-v">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-cpu" id="u-cpu-b" style="width:0%"></div></div>
        <div class="spark" id="u-cpu-s"></div>
      </div>
      <div class="ucard" id="uc-mem">
        <div class="uc-head" data-tip="RAM|Host memory usage from /proc/meminfo (used &divide; 60 GB total). Red at &ge;90%, yellow at &ge;70%."><span class="uc-ic">&#129504;</span><span class="uc-name">RAM</span><span class="uc-val" id="u-mem-v">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-mem" id="u-mem-b" style="width:0%"></div></div>
        <div class="spark" id="u-mem-s"></div>
      </div>
      <div class="ucard" id="uc-mio">
        <div class="uc-head" data-tip="Memory I/O|Page in/out rate from /proc/vmstat (pgpgin + pgpgout, in KB/s). High = heavy disk-backed memory pressure (swapping, file-backed pages). The sparkline tracks the last ~7.5 min."><span class="uc-ic">&#128192;</span><span class="uc-name">MEM I/O</span><span class="uc-val" id="u-mio-v">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-mio" id="u-mio-b" style="width:0%"></div></div>
        <div class="spark" id="u-mio-s"></div>
      </div>
      <div class="ucard" id="uc-pf">
        <div class="uc-head" data-tip="Page Faults|Minor page faults per second from /proc/vmstat. Major faults (disk) are shown in the value. High major faults = the system is reading from disk to satisfy memory references."><span class="uc-ic">&#9888;&#65039;</span><span class="uc-name">PFAULTS</span><span class="uc-val" id="u-pf-v">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-pf" id="u-pf-b" style="width:0%"></div></div>
        <div class="spark" id="u-pf-s"></div>
      </div>
      <div class="ucard" id="uc-gpu">
        <div class="uc-head" data-tip="GPU|Average SM utilization of both RTX 5070 Ti GPUs, from nvidia-smi. 99% = saturated. The optimization lever is vLLM parallelism / tensor-parallel, not more GPUs."><span class="uc-ic">&#127918;</span><span class="uc-name">GPU</span><span class="uc-val" id="u-gpu-v">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-gpu" id="u-gpu-b" style="width:0%"></div></div>
        <div class="spark" id="u-gpu-s"></div>
      </div>
      <div class="ucard" id="uc-vmem">
        <div class="uc-head" data-tip="VRAM|Average GPU memory (used &divide; 16303 MiB) across both GPUs. ~97% means the KV-cache is the bottleneck - true data-parallel (parallel=2) needs a second full model copy and would OOM."><span class="uc-ic">&#128190;</span><span class="uc-name">VRAM</span><span class="uc-val" id="u-vmem-v">--</span></div>
        <div class="bar-track"><div class="bar-fill fill-vram" id="u-vmem-b" style="width:0%"></div></div>
        <div class="spark" id="u-vmem-s"></div>
      </div>
    </div>
    <div class="util-proc">
      <div class="uc-head small"><span class="uc-ic">&#128202;</span> PROCESS CPU</div>
      <div class="procbars" id="procbars"></div>
    </div>
  </div>
</div>

<!-- Bottom bar: TODAY (big) / YESTERDAY (small) - starting point = local midnight (00:00), not any service boot -->
<div class="bar" style="flex-direction:column;align-items:stretch;height:auto;padding:8px 18px;gap:5px">
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <span style="font-size:.62em;color:var(--dim);text-transform:uppercase;letter-spacing:1px;min-width:46px">Today</span>
    <div class="m" data-tip="Tasks (today)|Context-gate tasks created since 00:00 today. A task = one proxied LLM request (one Goose turn). Small dim number = yesterday's count."><span class="l">Tasks</span><span class="n" id="m-tasks">0</span><span class="n small" id="m-tasks-y" style="color:var(--dim)"></span></div>
    <div class="m" data-tip="Events (today)|Context-gate events (messages/tool calls) created since 00:00 today. Each task produces several events."><span class="l">Events</span><span class="n" id="m-evt">0</span><span class="n small" id="m-evt-y" style="color:var(--dim)"></span></div>
    <div class="m" data-tip="Memories (today)|Durable memories extracted by the 4B worker since 00:00 today (active=true)."><span class="l">Memories</span><span class="n" id="m-mem">0</span><span class="n small" id="m-mem-y" style="color:var(--dim)"></span></div>
    <div class="m" data-tip="Knowledge (today)|Long-lived knowledge entries distilled since 00:00 today (active=true)."><span class="l">Knowledge</span><span class="n" id="m-know">0</span><span class="n small" id="m-know-y" style="color:var(--dim)"></span></div>
    <div class="m" data-tip="Jobs done (today)|Memory-extraction jobs that completed successfully since 00:00 today."><span class="l">Jobs done</span><span class="n" id="m-done">0</span><span class="n small" id="m-done-y" style="color:var(--dim)"></span></div>
    <div class="m" data-tip="Jobs failed (today)|Extraction jobs that failed since 00:00 today. Non-zero = the worker is erroring (check worker log / vLLM)."><span class="l">Jobs failed</span><span class="n" id="m-fail">0</span><span class="n small" id="m-fail-y" style="color:var(--dim)"></span></div>
  </div>
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <span style="font-size:.62em;color:var(--dim);text-transform:uppercase;letter-spacing:1px;min-width:46px">Activity</span>
    <div class="m" data-tip="Events (1h)|Events in the last hour - the liveness signal. 0 = the proxy/worker produced nothing in the last hour."><span class="l">Events 1h</span><span class="n blue" id="m-evt1h">0</span></div>
    <div class="m" data-tip="Total tasks|All tasks ever stored (not reset by any service restart)."><span class="l">Tasks total</span><span class="n small" id="m-tasks-t" style="color:var(--dim)">0</span></div>
    <div class="m" data-tip="Total events|All events ever stored (not reset by any service restart)."><span class="l">Events total</span><span class="n small" id="m-evt-t" style="color:var(--dim)">0</span></div>
    <div class="m" data-tip="Jobs pending|Extraction jobs queued, not yet started by the worker."><span class="l">Jobs pending</span><span class="n" id="m-pend">0</span></div>
    <div class="m" data-tip="Jobs processing|Extraction jobs the worker is currently running."><span class="l">Jobs processing</span><span class="n blue" id="m-proc">0</span></div>
    <div class="m" data-tip="Updated|When the dashboard last refreshed all panels (auto-polls every 3s)."><span class="l">Updated</span><span class="n small" id="m-upd">--</span></div>
  </div>
</div>

<div id="settingsModal" class="modal">
  <div class="mbox">
    <div class="m-head"><h2>&#9881; SETTINGS</h2><span class="close" onclick="closeSettings()">&#10005;</span></div>
    <div class="m-hint">
      <button class="m-tab active" id="tabRuntime" onclick="switchTab('runtime')">RUNTIME (proxy + worker)</button>
      <button class="m-tab" id="tabDashboard" onclick="switchTab('dashboard')">DASHBOARD (this service)</button>
      <span id="sbPath" style="color:var(--dim)"></span>
    </div>
    <div class="sb-table-wrap">
      <table class="sb-table" id="envTable">
        <thead><tr><th>Variable</th><th>Type</th><th>Value</th><th>Unit</th><th>Source</th></tr></thead>
        <tbody id="envTableBody"></tbody>
      </table>
    </div>
    <div class="m-foot">
      <span class="msg" id="settingsMsg"></span>
      <div>
        <button class="m-btn" onclick="reloadSettingsText()">RELOAD</button>
        <button class="m-btn save" onclick="saveSettings()">SAVE</button>
        <button class="m-btn restart" id="btnSaveRestart" onclick="saveAndRestart()">SAVE &amp; RESTART</button>
      </div>
    </div>
  </div>
</div>
<div id="logModal" class="modal">
  <div class="mbox">
    <div class="m-head"><h2>&#128203; LOG EXPLORER</h2><span class="close" onclick="closeLog()">&#10005;</span></div>
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
function setBox(id,st){var b=$('box-'+id);if(!b)return;b.className=b.className.replace(/\\b(up|down|degraded)\\b/g,'').trim();if(st==='up')b.classList.add('up');else if(st==='degraded')b.classList.add('degraded');else b.classList.add('down');}
function sv(id,t){var e=$(id);if(e)e.textContent=t==null?'--':t}
function spark(id,vals,max){var el=$(id);if(!el)return;if(!vals||vals.length<2){el.innerHTML='';return;}var W=140,H=26,n=vals.length;var pts=vals.map(function(v,i){var x=(i/(n-1))*W;var y=H-((Math.max(0,Math.min(max,v))/max)*H);return x.toFixed(1)+','+y.toFixed(1);}).join(' ');el.innerHTML='<svg width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="1.5" points="'+pts+'"/></svg>'}
function fmtKB(kb){if(kb==null)return'--';if(kb>=1048576)return(kb/1048576).toFixed(1)+'GB';if(kb>=1024)return(kb/1024).toFixed(0)+'MB';return kb+'KB'}
function fmtRate(kbs){if(kbs==null)return'--';if(kbs>=1024)return(kbs/1024).toFixed(1)+'MB/s';return kbs.toFixed(0)+'KB/s'}
function fmtUp(s){if(s==null)return'--';var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+'d ':'')+(h?h+'h ':'')+m+'m'}

async function poll(){
try{
var r=await fetch('/api/health');var d=await r.json();
var s=d.services||{};var db=d.db_metrics||{};var sys=d.system||{};

// --- Service boxes ---
setBox('pg',s.postgresql?s.postgresql.status:'down');
sv('pg-status',s.postgresql?s.postgresql.status.toUpperCase():'DOWN');
sv('pg-tasks',db.tasks_total!=null?db.tasks_total:'--');
sv('pg-ev1h',db.events_1h!=null?db.events_1h:'--');
sv('pg-mem',db.memories_total!=null?db.memories_total:'--');
sv('pg-know',db.knowledge_total!=null?db.knowledge_total:'--');

setBox('proxy',s.ctxgate_proxy?s.ctxgate_proxy.status:'down');
sv('px-status',s.ctxgate_proxy?s.ctxgate_proxy.status.toUpperCase():'DOWN');
sv('px-lat',s.ctxgate_proxy&&s.ctxgate_proxy.latency_ms!=null?s.ctxgate_proxy.latency_ms+'ms':'--');
sv('px-unit',s.ctxgate_proxy&&s.ctxgate_proxy.unit?s.ctxgate_proxy.unit:'ctxgate-proxy.service');

setBox('vllm',s.vllm?s.vllm.status:'down');
sv('vl-status',s.vllm?s.vllm.status.toUpperCase():'DOWN');
sv('vl-lat',s.vllm&&s.vllm.latency_ms!=null?s.vllm.latency_ms+'ms':'--');
var vm=d.vllm_metrics||{};
sv('vl-prefill',vm.prefill_tps!=null?vm.prefill_tps+' t/s':'--');
sv('vl-genreal',vm.gen_real_tps!=null?vm.gen_real_tps+' t/s':'--');
sv('vl-gentotal',vm.gen_total_tps!=null?vm.gen_total_tps+' t/s':'--');
sv('vl-reqs',(vm.running!=null?vm.running:'?')+' / '+(vm.waiting!=null?vm.waiting:'?'));
if(vm.kv_cache_pct!=null){sv('vl-kv',vm.kv_cache_pct.toFixed(1)+'%');$('b-kv').style.width=Math.min(100,vm.kv_cache_pct)+'%';}
else{sv('vl-kv','--');$('b-kv').style.width='0%';}
if(vm.prefix_hit_rate!=null){sv('vl-pfx',(vm.prefix_hit_rate*100).toFixed(1)+'%');$('b-pfx').style.width=Math.min(100,vm.prefix_hit_rate*100)+'%';}
else{sv('vl-pfx','--');$('b-pfx').style.width='0%';}

setBox('lm',s.mistral?s.mistral.status:'down');
sv('lm-status',s.mistral?s.mistral.status.toUpperCase():'DOWN');
sv('lm-lat',s.mistral&&s.mistral.latency_ms!=null?s.mistral.latency_ms+'ms':'--');
var wk=s.worker||{};
sv('lm-ctx',wk.lm_ctx_avg!=null?wk.lm_ctx_avg+' / '+(wk.lm_ctx_max||0)+' tok':'--');
var tokOut2=wk.lm_tokens_out||0;
sv('lm-req',wk.lm_rpm!=null?wk.lm_rpm+' req/min  '+(tokOut2>=1000?(tokOut2/1000).toFixed(1)+'k':tokOut2)+' tok':'--');

setBox('worker',s.worker?s.worker.status:'down');
sv('wk-status',s.worker?s.worker.status.toUpperCase():'DOWN');
sv('wk-pid',s.worker&&s.worker.pid?s.worker.pid:'--');
sv('wk-hb',s.worker&&s.worker.heartbeat_age_s!=null?s.worker.heartbeat_age_s+'s':'--');
sv('wk-proc',db.jobs_processing!=null?db.jobs_processing:0);
sv('wk-pend',db.jobs_pending!=null?db.jobs_pending:0);
sv('wk-done',db.jobs_done_today!=null?db.jobs_done_today:0);

// --- System panel (htop-style) ---
if(sys.cpu_pct!=null){
  sv('s-cpu',sys.cpu_pct.toFixed(1)+'%  ('+sys.cpu_cores+' cores)');
  $('b-cpu').style.width=Math.min(100,sys.cpu_pct)+'%';
}
var cb=$('corebars');
if(cb&&sys.cpu_per_core&&sys.cpu_per_core.length){
  cb.innerHTML=sys.cpu_per_core.map(function(v,i){
    var col=v>=90?'var(--red)':(v>=70?'var(--yellow)':'var(--cyan)');
    return '<div class="corebar" title="Core '+i+': '+v.toFixed(1)+'%"><div class="corebar-fill" style="width:'+Math.min(100,v)+'%;background:'+col+'"></div></div>';
  }).join('');
}
if(sys.mem_pct!=null){
  sv('s-mem',fmtKB(sys.mem_used_kb)+' / '+fmtKB(sys.mem_total_kb)+'  ('+sys.mem_pct.toFixed(1)+'%)');
  $('b-mem').style.width=Math.min(100,sys.mem_pct)+'%';
}
var mio=sys.mem_io||{};
sv('s-pg',fmtRate(mio.pgpgin_kbs||0)+' \u2193  '+fmtRate(mio.pgpgout_kbs||0)+' \u2191');
sv('s-pf',(mio.pgfault_ps||0).toFixed(0)+' minor  '+(mio.pgmajfault_ps||0).toFixed(1)+' major');
if(sys.swap_pct!=null){
  sv('s-swap',fmtKB(sys.swap_used_kb)+' / '+fmtKB(sys.swap_total_kb)+'  ('+sys.swap_pct.toFixed(1)+'%)');
  $('b-swap').style.width=Math.min(100,sys.swap_pct)+'%';
}else{
  sv('s-swap','none');
  $('b-swap').style.width='0%';
}
var psi2=sys.psi||{};
var psiM=psi2.memory||{},psiIo=psi2.io||{},psiC=psi2.cpu||{};
sv('s-psi',(psiM.some||0).toFixed(1)+'% some');
sv('s-psi-io',(psiIo.some||0).toFixed(1)+'% some');
sv('s-psi-cpu',(psiC.some||0).toFixed(1)+'% some');
sv('s-load',sys.load1!=null?(sys.load1.toFixed(2)+' / '+sys.load5.toFixed(2)+' / '+sys.load15.toFixed(2)):'--');
sv('s-uptime',fmtUp(sys.uptime_s));
sv('s-net',fmtRate(sys.net_rx_kbs)+' \u2193  '+fmtRate(sys.net_tx_kbs)+' \u2191');
var netTot=(sys.net_rx_kbs||0)+(sys.net_tx_kbs||0);
$('b-net').style.width=Math.min(100,netTot/10)+'%';
sv('s-disk',fmtRate(sys.disk_rd_kbs)+' \u2193  '+fmtRate(sys.disk_wr_kbs)+' \u2191');
var diskTot=(sys.disk_rd_kbs||0)+(sys.disk_wr_kbs||0);
$('b-disk').style.width=Math.min(100,diskTot/10)+'%';

// --- Top processes table (per-core CPU) ---
if(sys.procs&&sys.procs.length){
  var rows=sys.procs.map(function(p){
    var pcp=p.per_core_pct!=null?p.per_core_pct:0;var cpc=p.cpu_pct!=null?p.cpu_pct:0;var mp=p.mem_pct!=null?p.mem_pct:0;var cc=p.core_count&&p.core_count>1?(p.core_range+' ('+pcp.toFixed(1)+'%)'):cpc.toFixed(1)+'%';
    var cccol=pcp>=90?'var(--red)':(pcp>=70?'var(--yellow)':'var(--text)');
    return '<tr data-tip="PID '+p.pid+'|'+p.name+' &middot; '+(p.threads||0)+' threads &middot; pinned to cores '+(p.core_range||'?')+' ('+(p.core_count||1)+' cores) &middot; total CPU '+cpc+'% over 3s &middot; per-core '+pcp+'% &middot; RSS '+fmtKB(p.rss_kb)+'">'+
      '<td class="num">'+p.pid+'</td><td class="pname">'+p.name+'</td><td class="num">'+fmtKB(p.rss_kb)+'</td><td class="num">'+mp.toFixed(1)+'%</td><td class="num" style="color:'+cccol+'">'+cc+'</td></tr>';
  }).join('');
  $('procBody').innerHTML=rows;
}else{
  $('procBody').innerHTML='<tr><td colspan="5" style="color:var(--dim)">no data</td></tr>';
}

// --- Utilization panel (6 cards = optimize signal) ---
var g=d.gpu||{};
function uc(key,val){if(val==null)return;sv('u-'+key+'-v',val.toFixed(1)+'%');var b=$('u-'+key+'-b');if(b)b.style.width=Math.min(100,val)+'%';var cc=$('uc-'+key);if(cc)cc.style.borderColor=val>=90?'var(--red)':(val>=70?'var(--yellow)':'var(--border)');}
uc('cpu',sys.cpu_pct);
uc('mem',sys.mem_pct);
uc('gpu',g.util_avg);
uc('vmem',g.mem_avg);
var mio3=sys.mem_io||{};
var mioTot3=(mio3.pgpgin_kbs||0)+(mio3.pgpgout_kbs||0);
var mioPct3=Math.min(100,mioTot3);
if(mioTot3>0){
  sv('u-mio-v',fmtRate(mioTot3));
  var mb3=$('u-mio-b');if(mb3)mb3.style.width=mioPct3+'%';
  var mc3=$('uc-mio');if(mc3)mc3.style.borderColor=mioTot3>=80?'var(--red)':(mioTot3>=50?'var(--yellow)':'var(--border)');
}else{
  sv('u-mio-v','0 KB/s');
}
var pfTot3=(mio3.pgfault_ps||0)+(mio3.pgmajfault_ps||0);
var pfPct3=Math.min(100,pfTot3/10);
if(pfTot3>0){
  sv('u-pf-v',(mio3.pgfault_ps||0).toFixed(0)+'+'+(mio3.pgmajfault_ps||0).toFixed(1)+'M');
  var pfb3=$('u-pf-b');if(pfb3)pfb3.style.width=pfPct3+'%';
  var pfc3=$('uc-pf');if(pfc3)pfc3.style.borderColor=(mio3.pgmajfault_ps||0)>=10?'var(--red)':'';
}else{
  sv('u-pf-v','0/s');
}
var H=d.util_history||[];
spark('u-cpu-s',H.map(function(x){return x.cpu||0}),100);
spark('u-mem-s',H.map(function(x){return x.mem||0}),100);
spark('u-mio-s',H.map(function(x){return (x.pgin||0)+(x.pgout||0)||0}),100);
spark('u-pf-s',H.map(function(x){return (x.pgfault||0)/10||0}),100);
spark('u-gpu-s',H.map(function(x){return x.gpu_util||0}),100);
spark('u-vmem-s',H.map(function(x){return x.gpu_mem||0}),100);
var pu=d.process_util||{};var pb=$('procbars');
if(pb){var order=[['vllm','vLLM'],['goose','Goose'],['proxy','Proxy'],['worker','Worker']];pb.innerHTML=order.map(function(o){var p=pu[o[0]]||{};var v=p.cpu_pct!=null?p.cpu_pct:0;var col=v>=90?'var(--red)':(v>=70?'var(--yellow)':'var(--green)');return '<div class="pbar"><span class="pbar-name">'+o[1]+'</span><div class="bar-track"><div class="bar-fill" style="width:'+Math.min(100,v)+'%;background:'+col+'"></div></div><span class="pbar-val">'+v.toFixed(0)+'%</span></div>';}).join('');}

}catch(e){console.error('poll-main',e)}
try{
// --- Bottom bar (TODAY big / YESTERDAY small / totals) - INDEPENDENT try/catch ---
function barPair(id, today, yest){sv(id, today!=null?today:0);var y=$(id+'-y');if(y)y.textContent=yest!=null?('y '+yest):'';}
function barTot(id, v){var e=$(id);if(e)e.textContent=v!=null?v:0;}
function barSet(id, v, redIf){var e=$(id);if(!e)return;e.textContent=v!=null?v:0;var oc=e.className.replace(/\b(err)\b/g,'').trim();e.className=redIf&&v>0?oc+' err':oc;}
barPair('m-tasks', db.tasks_today, db.tasks_yest);
barPair('m-evt',   db.events_today, db.events_yest);
barPair('m-mem',   db.memories_today, db.memories_yest);
barPair('m-know',  db.knowledge_today, db.knowledge_yest);
barPair('m-done',  db.jobs_done_today, db.jobs_done_yest);
barSet('m-fail',   db.jobs_failed_today, true);
var fy=$('m-fail-y');if(fy)fy.textContent=db.jobs_failed_yest!=null?('y '+db.jobs_failed_yest):'';
barTot('m-tasks-t', db.tasks_total);
barTot('m-evt-t',   db.events_total);
sv('m-evt1h', db.events_1h!=null?db.events_1h:0);
sv('m-pend',  db.jobs_pending!=null?db.jobs_pending:0);
sv('m-proc',  db.jobs_processing!=null?db.jobs_processing:0);
if(d.last_update)sv('m-upd',new Date(d.last_update*1000).toLocaleTimeString());
}catch(e){console.error('poll-bar',e)}
}

async function ctrl(ev,n,a){
var b=ev.target;b.disabled=true;
try{
  var r=await fetch('/api/control/'+n+'/'+a,{method:'POST'});
  var d=await r.json();
  if(!d.ok)alert('Err: '+(d.error||'?'));
  else alert(n+' '+a+': ok'+(d.steps?'\n'+d.steps.join('\n'):''));
}catch(e){alert('Ctrl: '+e.message)}
b.disabled=false;
}

function tick(){$('clock').textContent=new Date().toLocaleTimeString()}
tick();setInterval(tick,1000);
poll();setInterval(poll,3000);

// --- Settings modal ---
var _sbTab='runtime';
var _sbPath={runtime:'',dashboard:''};
var ENV_VARS=[
["CTXGATE_DB_DSN","str","postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ctxproxy",""],
["CTXGATE_VLLM_URL","url","http://127.0.0.1:29000/v1",""],
["CTXGATE_VLLM_MODEL","str","Qwen3.8-27B",""],
["CTXGATE_LM_URL","url","https://api.mistral.ai/v1",""],
["CTXGATE_LM_MODEL","str","mistral-small-latest",""],
["CTXGATE_LM_TIMEOUT","int","120","s"],
["CTXGATE_MAX_CONTEXT","int","84000","tokens"],
["CTXGATE_MAX_INPUT","int","64000","tokens"],
["CTXGATE_MAX_OUTPUT","int","18000","tokens"],
["CTXGATE_SAFETY_MARGIN","int","2000","tokens"],
["CTXGATE_WALL_CLOCK_MAX","int","1800","s"],
["CTXGATE_MAX_CONTINUATIONS","int","5","count"],
["CTXGATE_WORKER_BACKPRESSURE","int","50","count"],
["CTXGATE_SESSION_TTL_HOURS","int","12","hours"],
["CTXGATE_MEMORY_TTL_DAYS","int","90","days"],
["CTXGATE_PROXY_PORT","int","9201","port"],
["CTXGATE_MAX_BODY_BYTES","int","20971520","bytes"],
["CTXGATE_API_KEY","str","",""],
["CTXGATE_VLLM_READ_TIMEOUT","int","300","s"],
["CTXGATE_VLLM_CONNECT_TIMEOUT","int","10","s"],
["CTXGATE_VLLM_WRITE_TIMEOUT","int","120","s"],
["CTXGATE_VLLM_POOL_TIMEOUT","int","30","s"],
["CTXGATE_MEMORY_WORKER","bool","1",""],
["CTXGATE_WORKER_POLL","float","2.0","s"],
["CTXGATE_WORKER_MAX_ATTEMPTS","int","3","count"],
["CTXGATE_WORKER_OUTAGE_TTL","float","1800","s"],
["CTXGATE_WORKER_MAX_TOKENS","int","512","tokens"],
["CTXGATE_WORKER_LOCK_TTL","float","30","s"]
];
var DASH_VARS=[
["CTXGATE_DASHBOARD_PORT","int","9202","port"],
["CTXGATE_DASHBOARD_HOST","str","127.0.0.1",""],
["CTXGATE_DASHBOARD_TOKEN","str","",""]
];
function switchTab(w){
  _sbTab=w;
  $('tabRuntime').classList.toggle('active',w==='runtime');
  $('tabDashboard').classList.toggle('active',w==='dashboard');
  $('btnSaveRestart').style.display=(w==='runtime')?'':'none';
  reloadSettingsText();
}
function openSettings(){$('settingsModal').classList.add('open');switchTab(_sbTab)}
function closeSettings(){$('settingsModal').classList.remove('open')}
function reloadSettingsText(){
  var msg=$('settingsMsg');msg.textContent='Loading ...';msg.style.color='';
  var url=(_sbTab==='runtime')?'/api/env':'/api/config';
  fetch(url).then(r=>r.json()).then(d=>{
    if(d.ok){_sbPath[_sbTab]=d.path||'';$('sbPath').textContent=d.path||'';msg.textContent=d.path||'';renderTable(d.text||'')}
    else{msg.textContent='Error: '+(d.error||'');msg.style.color='var(--red)'}
  }).catch(e=>{msg.textContent='Fetch failed: '+e;msg.style.color='var(--red)'});
}
function renderTable(envText){
  var vars=(_sbTab==='runtime')?ENV_VARS:DASH_VARS;
  var current={};
  envText.split('\n').forEach(function(line){
    var idx=line.indexOf('=');
    if(idx>0){current[line.substring(0,idx).trim()]=line.substring(idx+1).trim()}
  });
  var tbody=$('envTableBody');tbody.innerHTML='';
  vars.forEach(function(v){
    var name=v[0],type=v[1],def=v[2],unit=v[3];
    var val=current[name]!==undefined?current[name]:def;
    var isCustom=current[name]!==undefined&&current[name]!==def;
    var inputType=(type==='int')?'number':(type==='float')?'number':'text';
    var step=(type==='float')?'step="0.1"':(type==='int')?'step="1"':'';
    var min=(type==='int'||type==='float')?'min="0"':'';
    var unitCell=unit?'<span class="unit-label">'+unit+'</span>':'<span class="unit-label dim">—</span>';
    var tr=document.createElement('tr');
    tr.innerHTML='<td class="var-name">'+name+'</td>'
      +'<td><span class="type-badge type-'+type+'">'+type.toUpperCase()+'</span></td>'
      +'<td><input type="'+inputType+'" '+step+' '+min+' id="env_'+name+'" value="'+val.replace(/"/g,'&quot;')+'" placeholder="'+def+'"></td>'
      +'<td>'+unitCell+'</td>'
      +'<td><span class="src-badge '+(isCustom?'src-custom':'src-default')+'">'+(isCustom?'CUSTOM':'DEFAULT')+'</span></td>';
    tbody.appendChild(tr);
  });
}
function collectEnvText(){
  var vars=(_sbTab==='runtime')?ENV_VARS:DASH_VARS;
  var lines=[];
  vars.forEach(function(v){
    var el=$('env_'+v[0]);if(!el)return;
    var val=el.value.trim();
    if(v[1]==='int'){if(val&&!/^\d+$/.test(val)){el.style.borderColor='var(--red)';return}}
    else if(v[1]==='float'){if(val&&!/^\d+\.?\d*$/.test(val)){el.style.borderColor='var(--red)';return}}
    else if(v[1]==='bool'){if(val!=='0'&&val!=='1'){el.style.borderColor='var(--red)';return}}
    el.style.borderColor='';
    if(val!=='')lines.push(v[0]+'='+val);
  });
  return lines.join('\n')+'\n';
}
function _save(){
  var msg=$('settingsMsg');msg.textContent='Saving ...';msg.style.color='';
  var text=collectEnvText();
  var url=(_sbTab==='runtime')?'/api/env':'/api/config';
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})}).then(r=>r.json()).then(function(d){
    if(d.ok){msg.textContent='Saved: '+(d.path||url);msg.style.color='var(--green)'}
    else{msg.textContent='Error: '+(d.error||'');msg.style.color='var(--red)'}
    setTimeout(function(){msg.style.color=''},5000);
    return d;
  });
}
function saveSettings(){_save()}
function saveAndRestart(){
  var msg=$('settingsMsg');msg.textContent='Saving & restarting proxy + worker ...';msg.style.color='';
  var text=collectEnvText();
  fetch('/api/env/save-and-restart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})}).then(r=>r.json()).then(function(d){
    if(d.ok){
      var p=(d.restart&&d.restart.ctxgate_proxy&&d.restart.ctxgate_proxy.ok)?'ok':'fail';
      var w=(d.restart&&d.restart.worker&&d.restart.worker.ok)?'ok':'fail';
      msg.textContent='Saved. proxy='+p+' worker='+w;
      msg.style.color=(p==='ok'&&w==='ok')?'var(--green)':'var(--red)';
    }else{msg.textContent='Error: '+(d.error||'');msg.style.color='var(--red)'}
    setTimeout(function(){msg.style.color=''},8000);
  }).catch(function(e){msg.textContent='Failed: '+e;msg.style.color='var(--red)'});
}
document.addEventListener('keydown',function(e){if(e.key==='Escape'){closeSettings();closeLog()}});

// --- Log modal ---
var lgTimer=null;
function openLog(){
  $('logModal').classList.add('open');
  loadLog();
  if(lgTimer)clearInterval(lgTimer);
  lgTimer=setInterval(function(){if($('logModal').classList.contains('open'))loadLog()},2000);
}
function closeLog(){
  $('logModal').classList.remove('open');
  if(lgTimer){clearInterval(lgTimer);lgTimer=null}
}
function loadLog(){
  var src=$('lgSrc').value;
  fetch('/api/log?source='+src+'&lines=500').then(function(r){return r.json()}).then(function(d){
    var el=$('logText');var stat=$('lgStat');
    if(d.ok){
      var atBottom=el.scrollTop+el.clientHeight>=el.scrollHeight-60;
      var changed=(el._last!==d.text);
      el.textContent=d.text;el._last=d.text;
      if($('lgAuto').checked&&(atBottom||changed))el.scrollTop=el.scrollHeight;
      stat.textContent='updated '+new Date().toLocaleTimeString();
    }else{stat.textContent='error: '+(d.error||'unknown')}
  }).catch(function(e){$('lgStat').textContent='fetch failed: '+e});
}

// ── Deep-explanation tooltip engine (delegated; wide, no space saved) ──
(function(){
  var tt=document.createElement('div');tt.id='tt';document.body.appendChild(tt);
  var hideT=null;
  function showTip(el){
    var raw=el.getAttribute('data-tip');if(!raw)return;
    var i=raw.indexOf('|');
    var title=i>0?raw.slice(0,i):raw;
    var body=i>0?raw.slice(i+1):'';
    tt.innerHTML='<div class="th">'+title+'</div>'+(body?'<div class="tb">'+body+'</div>':'')+'<div class="tw">hover to inspect &middot; value updates live</div>';
    tt.style.display='block';
    var r=el.getBoundingClientRect();
    var tw=tt.offsetWidth,th=tt.offsetHeight;
    var left=r.left+8; if(left+tw>window.innerWidth-10)left=Math.max(10,window.innerWidth-tw-10);
    var top=r.bottom+8; if(top+th>window.innerHeight-10)top=Math.max(10,r.top-th-8);
    tt.style.left=left+'px';tt.style.top=top+'px';
  }
  function hideTip(){tt.style.display='none';}
  document.addEventListener('mouseover',function(e){
    if(hideT){clearTimeout(hideT);hideT=null;}
    var t=e.target;
    while(t&&t.nodeType===1){
      if(t.hasAttribute&&t.hasAttribute('data-tip')){showTip(t);return;}
      t=t.parentElement;
    }
    hideTip();
  },true);
  document.addEventListener('scroll',hideTip,true);
  window.addEventListener('resize',hideTip);
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    # Disable uvicorn's own signal handlers — the dashboard's own
    # sigwaitinfo-based handler (in app.py) is the canonical one.
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server.run()

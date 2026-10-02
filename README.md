# ctxgate-proxy — Local LLM Context-Gate Proxy

A self-contained local LLM infrastructure: a token-aware context-gate proxy
in front of vLLM, a 4B-model memory worker backed by PostgreSQL, and a
health-monitoring GUI with full service control. **Everything is operated
from the GUI (http://127.0.0.1:9202). Zero terminal, zero bash, zero fish.**

![ctxgate-proxy dashboard](docs/dashboard.png)
*The dashboard — health dots, live DB metrics, and one-click START / STOP / RESTART for every service.*

---

## 1. Full Data Flow

```
                        ┌────────────────────────────────────────────────────────┐
                        │                        HOST                            │
                        │                                                        │
  Goose agent ──POST /v1/chat/completions──► ┌──────────────────┐                │
  (any LLM client)                            │  ctxgate-proxy   │  :9201        │
                                            │  (FastAPI)        │                │
                                            │  - rolling-window │                │
                                            │    trimming (130k) │               │
                                            │  - token counting │                │
                                            │  - event capture  │                │
                                            └───┬─────┬──────┬───┘                │
              main inference                     │     │      │  async job        │
                    ┌────────────────────────────┘     │      │  enqueue          │
                    ▼                                  │      ▼                    │
        ┌─────────────────────┐                        │  ┌───────────────────┐   │
        │  vLLM :29000        │                        │  │   4B worker       │   │
        │  Qwen3.8-27B        │                        │  │ (polls memory_    │   │
        │  (main LLM)         │                        │  │  jobs, SKIP       │   │
        └─────────────────────┘                        │  │  LOCKED)          │   │
                                                       │  └──┬───────────┬────┘   │
        memory / knowledge /                              │           │          │
        tasks / events  ◄─────────────────────────────────┘           │          │
        ┌─────────────────────────┐                                   ▼          │
        │  PostgreSQL :5432       │                        ┌──────────────────┐  │
        │  DB: ctxproxy           │   durable memories     │  LM Studio :1234 │  │
        │  proxy.tasks            │   ◄─────────────────── │  qwen3-4b        │  │
        │  proxy.events           │   extract→QC→dedupe    │  (extraction     │  │
        │  proxy.memories         │   →supersede→store     │   + quality      │  │
        │  proxy.knowledge        │                        │   check only)    │  │
        │  proxy.memory_jobs      │                        └──────────────────┘  │
        └──────────┬──────────────┘                                              │
                   │ metrics (rows, job counts)                                  │
                   │ health (TCP 5432)                                           │
                   ▼                                                             │
  Browser ──► ┌─────────────────────────────────────────────────────────┐         │
  (you)       │  ctxgate-dashboard :9202                               │         │
              │  • red/yellow/green dots for all 5 monitored targets    │         │
              │  • database metrics card                               │         │
              │  • START / STOP / RESTART buttons for proxy & worker   │         │
              │  • feedback bar: step-by-step trace of each action     │         │
              └─────────────────────────────────────────────────────────┘         │
```

### What flows where

1. **Goose → proxy (:9201)**: every `/v1/chat/completions` request. The proxy
   trims the message list to fit the upstream context window (token-aware,
   dependency-graph safe), logs the event to PostgreSQL, enqueues a memory job,
   and forwards the request to vLLM.
2. **proxy → vLLM (:29000)**: the main 27B model. Synchronous — the client
   waits here.
3. **proxy → PostgreSQL (:5432)**: tasks, events, job enqueues.
4. **worker → PostgreSQL**: polls `proxy.memory_jobs` (FOR UPDATE SKIP LOCKED,
   parallelism 1), never blocks the main path.
5. **worker → LM Studio (:1234)**: the 4B model does extraction + self-quality-
   check on a compact payload. A bad job is discarded, never fatal. If LM Studio
   is down, jobs back off for up to 30 min, then are marked failed.
6. **worker → PostgreSQL**: dedupe / UPDATE / SUPERSEDE into `proxy.memories`.
7. **dashboard (:9202) → everything**: independent poller (every 300 s by
   default) checks PostgreSQL (TCP), vLLM (HTTP), LM Studio (HTTP), proxy
   (HTTP /health), worker (lock-file heartbeat) + DB row metrics. It is the
   **single source of truth for health**, and also the **only control surface**.

No service has any `After=`/`Wants=`/`Requires=` ordering dependency.
Each one is fully independent; if one dies, the dashboard shows a red dot.

---

## 2. Installation

### A. AUR package (recommended — the packaged solution)

```
# build + install locally
cd /home/user/ctxproxy/pkg/ctxgate-proxy
makepkg -si

# or, once published to AUR (git repo + yay submission):
yay -S ctxgate-proxy
```

The package installs:

| Path | What |
|---|---|
| `/opt/ctxgate-proxy/` | code (proxy, worker, dashboard) + self-contained venv |
| `/etc/ctxgate-proxy/.env` | configuration (mode 600) |
| `/var/log/ctxgate-proxy/` | proxy.log, worker.log, dashboard.log |
| `/usr/lib/systemd/system/ctxgate-{proxy,worker,dashboard}.service` | 3 system services, zero ordering deps |

After install:

```
# 1. edit config
sudo editor /etc/ctxgate-proxy/.env     # set CTXGATE_DB_DSN, model names, URLs

# 2. enable + start (independent — no ordering between them)
sudo systemctl enable --now ctxgate-proxy
sudo systemctl enable --now ctxgate-worker
sudo systemctl enable --now ctxgate-dashboard

# 3. open the GUI
#    http://127.0.0.1:9202
```

### B. User-level services (current live setup)

Already running under `~/.config/systemd/user/ctxproxy-{proxy,worker,dashboard}.service`
with `EnvironmentFile=/home/user/ctxproxy/.env`. To migrate to the AUR
system-level setup:

```
systemctl --user disable --now ctxproxy-proxy ctxproxy-worker
# (dashboard stays for the GUI, or migrate it too)
```

Then use the AUR services. From that moment **all control is in the GUI**.

### Configuration (`/etc/ctxgate-proxy/.env` or `~/.ctxproxy/.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CTXGATE_DB_DSN` | — (required) | PostgreSQL DSN for the ctxproxy DB |
| `CTXGATE_VLLM_URL` | http://127.0.0.1:29000/v1 | main LLM |
| `CTXGATE_VLLM_MODEL` | Qwen3.8-27B | served model name |
| `CTXGATE_LM_URL` | http://127.0.0.1:1234/v1 | 4B LM Studio (suffix auto-normalized) |
| `CTXGATE_LM_MODEL` | qwen3-4b-instruct-2507 | extraction model |
| `CTXGATE_WORKER_POLL` | 2.0 | worker poll interval (s) |
| `CTXGATE_WORKER_MAX_ATTEMPTS` | 3 | per-job retries |
| `CTXGATE_WORKER_OUTAGE_TTL` | 1800 | backoff window before jobs fail |
| `CTXGATE_DASHBOARD_PORT` | 9202 | GUI port |
| `CTXGATE_DASHBOARD_POLL` | 300 | health poll interval (s) |

---

## 3. The GUI (http://127.0.0.1:9202)

One dark single-page dashboard, auto-refreshes every 10 s. Three regions:

### 3.1 Services card — health dots

Five monitored targets, one row each: **dot + name + status + detail**.

| Target | Dot | What the dot means | How it is checked |
|---|---|---|---|
| `postgresql` | 🟢 / 🔴 | DB reachable / not | TCP connect to 127.0.0.1:5432 |
| `vllm` | 🟢 / 🟡 / 🔴 | main LLM up (ms latency) / non-200 / unreachable | GET :29000/v1/models |
| `lm_studio` | 🟢 / 🟡 / 🔴 | 4B model up / non-200 / unreachable | GET :1234/v1/models |
| `ctxgate_proxy` | 🟢 / 🟡 / 🔴 | proxy up (ms) / non-200 / unreachable — **controllable** | GET :9201/health |
| `worker` | 🟢 / 🟡 / 🔴 | fresh heartbeat (<30 s) / stale (30–120 s) / dead (>120 s) — **controllable** | lock-file heartbeat |

- **🟢 up** — healthy.
- **🟡 degraded** — alive but not fully correct: non-200 HTTP code, stale
  worker heartbeat, high-latency hint in parentheses.
- **🔴 down** — dead: connection refused, timeout, missing/stale lock file.
  The error reason is shown inline after `-`.

### 3.2 Buttons (on the two controllable rows)

| Button | Behaviour |
|---|---|
| **START** (green) | Idempotent start. If the port/lock is already held by the service, does nothing. If the port is held by a *stray* process, the stray is SIGKILLed first, then a clean start. Verifies health before reporting OK. |
| **STOP** (orange) | Clean stop, then SIGKILL of any leftover main PID, then force-frees the port (proxy) or removes the lock file (worker). After STOP the service is **not** running. |
| **RESTART** (red) | **Kill-forever + fresh start.** Glows solid red as long as the service is NOT healthy, so you always see what needs it. Sequence (all server-side, all shown in the feedback bar): 1) clean stop → 2) SIGKILL leftover main PID → 3) loop: SIGKILL whatever holds the port until it is provably free (up to 30 s) → 4) remove stale lock file → 5) `reset-failed` (clears crash-loop counters so a fresh start can never be blocked) → 6) fresh start → 7) **verify** (proxy: HTTP 200 from /health; worker: fresh heartbeat < 30 s). Reports `ok: false` if any step fails. |

### 3.3 Feedback bar

Below the cards. Shows the last control action and its full step trace, e.g.:

```
OK: ctxgate_proxy restart done  ||  stop rc=0 -> port 9201: free -> reset-failed rc=0 -> start rc=0 -> verify: HTTP 200
```

Buttons disable while an action runs (an action can take a few seconds; the
10 s page refresh does not cancel it — it continues server-side).

### 3.4 Database Metrics card

Live counts from PostgreSQL: `tasks`, `events`, `memories` (active),
`knowledge` (active), `jobs_pending`, `jobs_processing`, `jobs_done`,
`jobs_failed`. A spike in `jobs_failed` with `lm_studio` red tells you the
4B model is down; the worker is safely backing off, nothing is lost.

### 3.5 Timestamp

`Updated: <time>` — when the dashboard's poller last wrote the health state.

---

## 4. Operating model (zero terminal)

| Situation | What you do in the GUI |
|---|---|
| Everything green | Nothing. |
| Proxy red/yellow | Click **RESTART** on the `ctxgate_proxy` row. Kill-forever + fresh start + verified. |
| Worker red/yellow | Click **RESTART** on the `worker` row. |
| You stopped something and want it back | Click **START** on that row. |
| PostgreSQL / vLLM / LM Studio red | No button — those are not part of this package (external: PG server, your vLLM, LM Studio). Their dots tell *you* what to check; the proxy/worker degrade gracefully without them. |

The dashboard itself is a systemd service with `Restart=on-failure` — if it
crashes, it comes back in 5 s. You never type a command.

---

## 5. Files

```
ctxproxy/
├── proxy/app.py            # :9201 context-gate proxy (FastAPI)
├── worker/worker.py        # 4B memory worker (async, lock-file heartbeat)
├── dashboard/dashboard.py  # :9202 GUI + health poller + control API
├── schema/00X_*.sql        # PostgreSQL schema (canonical — 001–009, apply in order)
├── .env / .env.example     # configuration
└── pkg/ctxgate-proxy/      # AUR package (PKGBUILD, .SRCINFO, tarball, systemd/)

> **Canonical schema directory:** `schema/` — all migrations (001–009) are numbered and applied in order. There is no separate `migrations/` directory.
```

Control API (what the buttons call, for reference):
`POST /api/control/{ctxgate_proxy|worker}/{start|stop|restart}`


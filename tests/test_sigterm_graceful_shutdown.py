#!/usr/bin/env python3
"""SIGTERM diagnostics + graceful-shutdown test for local-llm-ctxgate-proxy.

Reproduces the reported production bug: an EXTERNAL process sends SIGTERM to the
proxy right after a stream starts (finish=tool_calls). The old _sigterm_handler
only logged its own PPid (the parent, not the sender) and then hard-killed the
process (SIG_DFL + os.kill(self)), defeating uvicorn graceful shutdown.

This test verifies the fix in proxy/app.py:
  1. The proxy is spawned as its own process.
  2. A streaming /v1/chat/completions request is opened so a request is
     genuinely in-flight.
  3. SIGTERM is sent FROM A KNOWN CHILD PROCESS (so the sender pid is
     deterministic and we can assert the proxy logged *that* pid).
  4. The proxy's log names the sender pid + cmdline (not just the parent).
  5. The in-flight stream DRAINS (client receives [DONE]) before the process
     exits, and the process exits within timeout_graceful_shutdown + 2 s.

Runs on its OWN ports (proxy 9203, mock vLLM 29301) so it never touches the
live 9201 proxy. Skips cleanly if the project .env has no reachable DB DSN.
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
sys.path.insert(0, ROOT)

PROXY_PORT = 9203          # distinct from the live proxy (9201) and dashboard (9202)
MOCK_PORT = 29301          # distinct from the live mock (29100)
GRACE = 3                  # must equal proxy/app.py uvicorn.Config(timeout_graceful_shutdown=3)
EXIT_BUDGET = GRACE + 2    # acceptance: exit within timeout_graceful_shutdown + 2 s
HEALTH_BUDGET = 35
LOG_PATH = "/tmp/ctxgate_sigterm_test.log"


def read_dsn() -> str:
    """Parse CTXGATE_DB_DSN straight from the project .env (real value)."""
    env_path = os.path.join(ROOT, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CTXGATE_DB_DSN="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v and v != "CHANGE_ME":
                        return v
    except OSError:
        pass
    return ""


def _free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def _wait_port(port: int, timeout: float = 15.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if _free(port):
            return True
        time.sleep(0.3)
    return _free(port)


def _http_get(url: str, timeout: float = 3.0) -> int:
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status


def _start_mock() -> subprocess.Popen:
    logf = open("/tmp/ctxgate_sigterm_mock.log", "w")
    return subprocess.Popen(
        [sys.executable, os.path.join(HERE, "mock_vllm.py"), "--port", str(MOCK_PORT)],
        stdout=logf, stderr=subprocess.STDOUT,
    )


def _start_proxy(dsn: str, logf) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "CTXGATE_PROXY_PORT": str(PROXY_PORT),
        "CTXGATE_VLLM_URL": "http://127.0.0.1:%d/v1" % MOCK_PORT,
        "CTXGATE_VLLM_MODEL": "Qwen3.8-27B",
        "CTXGATE_MEMORY_WORKER": "0",
        "CTXGATE_DB_DSN": dsn,
        "PYTHONUNBUFFERED": "1",
    })
    return subprocess.Popen(
        [sys.executable, "-u", os.path.join(ROOT, "proxy", "app.py")],
        stdout=logf, stderr=subprocess.STDOUT, cwd=ROOT, env=env,
    )




def _open_inflight_stream(marker: str):
    """Open a streaming request; return (conn, got_first_chunk)."""
    conn = http.client.HTTPConnection("127.0.0.1", PROXY_PORT, timeout=EXIT_BUDGET + 5)
    body = json.dumps({
        "model": "Qwen3.8-27B",
        "stream": True,
        "messages": [{"role": "user", "content": marker}],
    })
    conn.request("POST", "/v1/chat/completions", body=body,
                 headers={"Content-Type": "application/json", "X-Session-ID": "sigterm-test"})
    resp = conn.getresponse()
    # Read the first chunk so we know the response has started flowing.
    first = b""
    while b"data:" not in first:
        chunk = resp.read(64)
        if not chunk:
            break
        first += chunk
    return conn, resp, (resp.status == 200 and b"data:" in first)


def test_sigterm_graceful_shutdown_and_sender_diagnostics():
    dsn = read_dsn()
    if not dsn:
        import pytest
        pytest.skip("no CTXGATE_DB_DSN in project .env; cannot start proxy")

    assert _free(PROXY_PORT), "port %d not free" % PROXY_PORT
    assert _free(MOCK_PORT), "port %d not free" % MOCK_PORT

    logf = open(LOG_PATH, "w")
    mock = _start_mock()
    proxy = _start_proxy(dsn, logf)

    try:
        # Wait for the mock to be ready.
        assert _wait_port(MOCK_PORT), "mock vLLM did not bind %d" % MOCK_PORT
        # Wait for the proxy to become healthy.
        t0 = time.monotonic()
        healthy = False
        while time.monotonic() - t0 < HEALTH_BUDGET:
            try:
                if _http_get("http://127.0.0.1:%d/health" % PROXY_PORT) == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        assert healthy, "proxy did not become healthy within %ds (see %s)" % (HEALTH_BUDGET, LOG_PATH)
        proxy_pid = proxy.pid

        # Open an in-flight streaming request.
        conn, resp, started = _open_inflight_stream("[OUT_25K]")
        assert started, "in-flight stream did not start (no first chunk)"

        # Give the stream a moment to be genuinely in-flight, then send SIGTERM
        # from a KNOWN child process (whose pid we record for the assertion).
        time.sleep(1.0)
        killer = subprocess.Popen(
            [sys.executable, "-c",
             "import os,signal; os.kill(%d, signal.SIGTERM)" % proxy_pid],
        )
        killer_pid = killer.pid
        killer.wait(timeout=10)  # the killer exits immediately after sending

        # The in-flight stream must drain: the client receives [DONE].
        resp_done = {"done": False}
        def _drain():
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if b"[DONE]" in line:
                        resp_done["done"] = True
                        break
            except Exception:
                resp_done["done"] = False
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        dthread = threading.Thread(target=_drain, daemon=True)
        dthread.start()
        dthread.join(timeout=GRACE + 5)
        assert resp_done["done"], "in-flight stream did not complete ([DONE] not received) before/after graceful shutdown"

        # The process must exit within the graceful-shutdown budget.
        try:
            rc = proxy.wait(timeout=EXIT_BUDGET)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()
            logf.close()
            with open(LOG_PATH) as f:
                tail = f.read()[-4000:]
            raise AssertionError("proxy did not exit within %ds of SIGTERM. tail: %s" % (EXIT_BUDGET, tail))

        logf.close()
        with open(LOG_PATH) as f:
            logtext = f.read()

        # --- Assertions on the diagnostics ---
        # 1) The sender's pid (the child that sent SIGTERM) is named in the log.
        assert ("sender_pid=%d" % killer_pid) in logtext, (
            "sender pid %d not found in log. Log tail: %s" % (killer_pid, logtext[-3000:])
        )
        # 2) A cmdline is recorded (the killer was python -c '...kill...').
        assert "sender_cmdline=" in logtext, "sender_cmdline missing from log"
        # 3) Graceful drain is evident (uvicorn waiting / finishing, not a hard kill).
        assert re.search(r"(Waiting for connections to close|Shutting down|Finished server process)", logtext), (
            "no graceful-shutdown markers in log (looked like a hard kill). tail: %s" % logtext[-3000:]
        )
        # 4) The in-flight count was reported at the moment of the signal.
        assert "inflight=" in logtext, "inflight= missing from the SIGNAL RECEIVED line"
    finally:
        for p in (proxy, mock):
            try:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)
            except Exception:
                pass
        try:
            logf.close()
        except Exception:
            pass
        # Clean up the test ports so the next run is clean.
        for port in (PROXY_PORT, MOCK_PORT):
            _wait_port(port, timeout=10)

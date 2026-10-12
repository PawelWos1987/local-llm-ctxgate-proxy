"""Background watchdog loops: systemd ping, memory worker, fd hygiene."""
import asyncio
import logging
import os
import time

log = logging.getLogger("ctxgate-proxy")


def _sd_notify(msg: str) -> None:
    """Send an sd_notify message to systemd if NOTIFY_SOCKET is set."""
    import socket as _socket
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall(msg.encode())
        s.close()
    except OSError:
        pass


async def _watchdog_loop():
    """Ping systemd every 10s. WatchdogSec=60 in the unit means the proxy
    is killed and restarted if the event loop cannot schedule this task
    for 60 seconds — that is the deadlock detector."""
    while True:
        try:
            _sd_notify("WATCHDOG=1")
        except Exception as _e:
            log.warning("watchdog ping failed: %s", _e)
        await asyncio.sleep(10)


async def _memory_worker_loop():
    """DEPRECATED: Memory extraction is handled by worker/worker.py (dedicated process).
    This loop is kept as a no-op to avoid racing with the dedicated worker.
    The dedicated worker uses FOR UPDATE SKIP LOCKED for safe concurrent access.
    """
    log.info("Memory worker loop: DISABLED (dedicated worker.py handles extraction + recovery)")
    while True:
        await asyncio.sleep(60)


async def _fd_hygiene_loop():
    """Monitor fd count and actively prevent exhaustion.

    Thresholds are RELATIVE to the actual RLIMIT_NOFILE soft limit:
      - 25%: log WARNING + fd_breakdown()
      - 50%: SWAP-THEN-DRAIN httpx clients (build new, swap ref, drain old after 60s)
      - 75%: graceful restart (reject new, drain in-flight up to 30s, os._exit(1))

    The loop body is wrapped in try/except so it never dies silently.
    A supervisor check in the health loop recreates the task if it dies.
    """
    import resource

    # FD hygiene constants
    FD_WARN_FRAC = 0.25
    FD_RECYCLE_FRAC = 0.50
    FD_RESTART_FRAC = 0.75
    FD_DRAIN_DELAY = 60
    FD_RESTART_DRAIN = 30

    while True:
        try:
            await asyncio.sleep(60)

            # Evict stale sessions
            from proxy.utils.worker import _evict_stale_sessions
            try:
                await _evict_stale_sessions()
            except Exception as e:
                log.warning("_evict_stale_sessions failed: %s", e)

            # Compute thresholds from the actual soft limit
            soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            warn_at = int(soft_limit * FD_WARN_FRAC)
            recycle_at = int(soft_limit * FD_RECYCLE_FRAC)
            restart_at = int(soft_limit * FD_RESTART_FRAC)

            fd_count = len(os.listdir("/proc/self/fd"))
            from proxy.hotpath.tracking import _fd_breakdown
            breakdown = _fd_breakdown()

            # Lazy import for mutable globals
            import proxy.app as _app

            if fd_count >= restart_at:
                log.critical("FD RESTART: %d fds >= %d (75%% of limit %d). Graceful restart.",
                             fd_count, restart_at, soft_limit)
                log.critical("FD breakdown at restart: %s", breakdown)
                _app._shutting_down = True
                for _ in range(FD_RESTART_DRAIN * 10):
                    if _app._inflight_count <= 0:
                        break
                    await asyncio.sleep(0.1)
                log.critical("FD RESTART: in-flight=%d, exiting.", _app._inflight_count)
                os._exit(1)

            elif fd_count >= recycle_at:
                log.warning("FD RECYCLE: %d fds >= %d (50%% of limit %d). Swap-then-drain.",
                            fd_count, recycle_at, soft_limit)
                log.warning("FD breakdown at recycle: %s", breakdown)
                from proxy.utils.vllm import _make_vllm_client
                from proxy.lm.client import _make_lm_client
                new_vllm = _make_vllm_client()
                new_lm = _make_lm_client()
                old_vllm = _app._vllm_client
                old_lm = _app._lm_client
                _app._vllm_client = new_vllm
                _app._lm_client = new_lm
                log.info("FD recycle: clients swapped. Draining old clients in %ds.", FD_DRAIN_DELAY)
                async def _drain_old(ov=old_vllm, ol=old_lm):
                    await asyncio.sleep(FD_DRAIN_DELAY)
                    for c in (ov, ol):
                        if c is not None:
                            try:
                                await c.aclose()
                            except Exception as e:
                                log.debug("FD drain: aclose failed: %s", e)
                asyncio.create_task(_drain_old())

            elif fd_count >= warn_at:
                log.warning("FD WARN: %d fds >= %d (25%% of limit %d). breakdown=%s",
                            fd_count, warn_at, soft_limit, breakdown)

        except Exception as e:
            log.exception("FD hygiene loop error (continuing): %s", e)

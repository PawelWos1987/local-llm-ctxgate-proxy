"""vLLM HTTP client management."""
import logging
import httpx

log = logging.getLogger("ctxgate-proxy")


def _get_vllm_client():
    """Accessor: return the current vLLM httpx client (safe across swaps)."""
    from proxy.app import _vllm_client
    return _vllm_client


def _make_vllm_client():
    """Build a fresh vLLM httpx client with bounded pool."""
    from proxy.app import _env_int
    t = httpx.Timeout(
        _env_int("CTXGATE_VLLM_READ_TIMEOUT", 300),
        connect=_env_int("CTXGATE_VLLM_CONNECT_TIMEOUT", 10),
        write=_env_int("CTXGATE_VLLM_WRITE_TIMEOUT", 120),
        pool=_env_int("CTXGATE_VLLM_POOL_TIMEOUT", 30),
    )
    return httpx.AsyncClient(timeout=t, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))

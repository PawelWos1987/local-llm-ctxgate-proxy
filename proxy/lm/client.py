"""LM (Mistral) HTTP client management."""
import logging
import httpx

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


def _get_lm_client():
    """Accessor: return the current LM/Mistral httpx client (safe across swaps)."""
    # TODO(I4): replace with AppContext
    from proxy.app import _lm_client
    return _lm_client

def _make_lm_client():
    """Build a fresh LM/Mistral httpx client with bounded pool."""
    # TODO(I4): replace with AppContext
    from proxy.app import MISTRAL_TIMEOUT, MISTRAL_API_KEY
    t = httpx.Timeout(MISTRAL_TIMEOUT, connect=10)
    return httpx.AsyncClient(timeout=t, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                             headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"} if MISTRAL_API_KEY else {})


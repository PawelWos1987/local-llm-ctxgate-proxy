"""OpenAI-compatible /v1/models passthrough to vLLM."""
import logging

import httpx
from fastapi import Request

from proxy.config import VLLM_URL

log = logging.getLogger("ctxgate-proxy")


async def v1_models(request: Request):
    """OpenAI-compatible /v1/models passthrough to vLLM.
    Prevents 404 when clients (dashboards, health checkers, Goose)
    probe the proxy with the standard OpenAI models endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(VLLM_URL.rstrip("/") + "/models")
            if resp.status_code == 200:
                return resp.json()
            return {"object": "list", "data": []}
    except Exception:
        return {"object": "list", "data": []}

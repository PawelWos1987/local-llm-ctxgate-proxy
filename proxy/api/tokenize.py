"""Diagnostic tokenize endpoint."""
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from proxy.config import VLLM_MODEL, ESTIMATOR_MODE
from proxy.token.counter import _rho_var

log = logging.getLogger("ctxgate-proxy")


async def tokenize_endpoint(request: Request):
    """Diagnostic: tokenize text using the active estimator mode.
    NOT safe for admission. Debugging/calibration only.
    """
    text = request.query_params.get("text", "")
    model = request.query_params.get("model", VLLM_MODEL)
    if not text:
        return JSONResponse({"error": "text parameter required"}, status_code=400)
    if len(text) > 100000:
        return JSONResponse({"error": "text too long (max 100000 chars)"}, status_code=413)
    char_count = len(text)  # diagnostic: char count only (token estimation removed)
    return {
        "text_length": len(text),
        "char_count": char_count,
        "mode": ESTIMATOR_MODE,
        "rho": _rho_var.get(),
        "warning": "Proxy-side estimate only. Not vLLM actual tokenization. Not safe for admission."
    }

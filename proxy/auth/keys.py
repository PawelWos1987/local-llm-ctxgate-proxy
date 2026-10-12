"""API key parsing and validation."""
import hashlib
import hmac
import logging

from proxy.config.constants import CTXGATE_API_KEYS, API_KEY, CTXGATE_ADMIN_KEY

log = logging.getLogger("ctxgate-proxy")


def _parse_api_keys() -> dict:
    """Parse CTXGATE_API_KEYS into {key: label} dict.
    Supports both 'label=key' format and legacy bare keys.
    """
    keys = {}
    raw = CTXGATE_API_KEYS
    if not raw:
        # Legacy: CTXGATE_API_KEY (single or comma-separated)
        if API_KEY:
            for k in API_KEY.split(","):
                k = k.strip()
                if k:
                    keys[k] = "local"
        return keys
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            label, key = part.split("=", 1)
            label = label.strip()
            key = key.strip()
            if label and key:
                # Validate label format
                import re as _re_auth
                if not _re_auth.match(r'^[a-z0-9_-]{1,32}$', label):
                    log.warning("Invalid API key label '%s' (must match [a-z0-9_-]{1,32})", label)
                    continue
                keys[key] = label
        else:
            # Bare key (legacy)
            keys[part] = "local"
    return keys

_api_key_map: dict = {}  # key -> label, populated at startup



def _get_valid_keys() -> set:
    return set(_api_key_map.keys())



def _compute_tenant_id(api_key: str) -> str:
    """Derive a stable 16-hex-char tenant ID from the API key label."""
    label = _api_key_map.get(api_key, "local")
    return hashlib.sha256(label.encode()).hexdigest()[:16]



def _validate_key(provided_key: str) -> tuple:
    """Validate an API key. Returns (is_valid, is_admin, tenant_id)."""
    if not provided_key:
        return (False, False, "")
    if CTXGATE_ADMIN_KEY and hmac.compare_digest(provided_key, CTXGATE_ADMIN_KEY):
        return (True, True, _compute_tenant_id(provided_key))
    if provided_key in _api_key_map:
        return (True, False, _compute_tenant_id(provided_key))
    return (False, False, "")

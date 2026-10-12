"""Bounded LRU cache for token counts of immutable components.

Key: SHA-256(canonical_bytes) + model + tokenizer_ver + template_ver + schema_ver
Value: token count (int)

Used in calibrated mode to avoid re-estimating unchanged components.
In exact mode, the tokenizer is authoritative and the cache is a
pure performance optimization.
"""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict

_TOKENIZER_VERSION = os.environ.get("CTXGATE_TOKENIZER_VERSION", "1")
_TEMPLATE_VERSION = os.environ.get("CTXGATE_TEMPLATE_VERSION", "1")
_SCHEMA_VERSION = os.environ.get("CTXGATE_SCHEMA_VERSION", "1")
_TOKEN_CACHE_MAX = int(os.environ.get("CTXGATE_TOKEN_CACHE_MAX", "10000"))


class _TokenCountCache:
    """Bounded LRU cache for token counts of immutable components.

    Key: SHA-256(canonical_bytes) + model + tokenizer_ver + template_ver + schema_ver
    Value: token count (int)

    Used in calibrated mode to avoid re-estimating unchanged components.
    In exact mode, the tokenizer is authoritative and the cache is a
    pure performance optimization.
    """
    def __init__(self, maxsize: int = _TOKEN_CACHE_MAX):
        self._cache: OrderedDict = OrderedDict()
        self._maxsize = maxsize
        self.hits = 0
        self.misses = 0

    def _make_key(self, content: str, model: str) -> str:
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return f"{h}:{model}:{_TOKENIZER_VERSION}:{_TEMPLATE_VERSION}:{_SCHEMA_VERSION}"

    def get(self, content: str, model: str) -> int | None:
        key = self._make_key(content, model)
        if key in self._cache:
            self._cache.move_to_end(key)
            self.hits += 1
            return self._cache[key]
        self.misses += 1
        return None

    def put(self, content: str, model: str, tokens: int) -> None:
        key = self._make_key(content, model)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = tokens
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def invalidate_model(self, model: str) -> None:
        """Invalidate all entries for a given model."""
        suffix = f":{model}:{_TOKENIZER_VERSION}:{_TEMPLATE_VERSION}:{_SCHEMA_VERSION}"
        to_remove = [k for k in self._cache if k.endswith(suffix)]
        for k in to_remove:
            del self._cache[k]

    def clear(self) -> None:
        self._cache.clear()
        self.hits = 0
        self.misses = 0


_token_cache = _TokenCountCache()

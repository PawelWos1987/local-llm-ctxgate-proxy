"""Database pool management: asyncpg pool creation and retry logic.

The actual pool creation happens in the lifespan handler in app.py.
This module provides reusable pool creation and retry helpers.
"""
import asyncio
import logging
import time

import asyncpg

log = logging.getLogger("ctxgate-proxy")

# TODO(I4): replace with AppContext


async def create_pool(dsn: str, min_size: int = 2, max_size: int = 10, retries: int = 5) -> asyncpg.Pool:
    """Create an asyncpg connection pool with retry logic."""
    last_err = None
    for attempt in range(retries):
        try:
            return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        except (OSError, asyncpg.PostgresError) as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            log.warning("DB pool creation attempt %d/%d failed: %s (retrying in %ds)",
                        attempt + 1, retries, e, wait)
            await asyncio.sleep(wait)
    raise last_err


async def retry_pool_operation(operation, *args, retries: int = 3, **kwargs):
    """Retry a pool operation with backoff on transient errors."""
    last_err = None
    for attempt in range(retries):
        try:
            return await operation(*args, **kwargs)
        except (asyncpg.InterfaceError, asyncio.TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise last_err

"""PostgreSQL connection pool using asyncpg."""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg
from config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# On deploy, Postgres often accepts TCP (healthcheck passes) while still in
# recovery ("the database system is starting up", SQLSTATE 57P03) and rejects
# queries. Without a retry the very first pool creation in the lifespan crashes
# with CannotConnectNowError → "Application startup failed. Exiting." → the
# container is marked unhealthy and the whole deploy fails. Retry briefly so a
# transient not-ready window is tolerated. Only the first (startup) call pays
# this; afterwards the cached pool is returned immediately.
_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "30"))
_CONNECT_RETRY_DELAY = float(os.getenv("DB_CONNECT_RETRY_DELAY", "2.0"))


async def get_pool() -> asyncpg.Pool:
    """Get or create the connection pool, waiting for Postgres to be ready."""
    global _pool
    if _pool is not None:
        return _pool

    last_exc: Exception | None = None
    for attempt in range(1, _CONNECT_RETRIES + 1):
        try:
            _pool = await asyncpg.create_pool(
                settings.DATABASE_URL, min_size=1, max_size=5
            )
            logger.info("Database pool created")
            return _pool
        except (OSError, asyncpg.PostgresError) as exc:
            # OSError → TCP not accepting yet; PostgresError (incl.
            # CannotConnectNowError) → server up but not ready to serve.
            last_exc = exc
            logger.warning(
                "Database not ready (attempt %d/%d): %s — retrying in %.1fs",
                attempt,
                _CONNECT_RETRIES,
                exc,
                _CONNECT_RETRY_DELAY,
            )
            await asyncio.sleep(_CONNECT_RETRY_DELAY)

    raise RuntimeError(
        f"Could not connect to the database after {_CONNECT_RETRIES} attempts"
    ) from last_exc


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

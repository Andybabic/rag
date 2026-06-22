"""Shared asyncpg pool management.

A single pool is shared across the resolver (per-usecase config/prompts/skills)
and the auth module (users). Each service can inject its own pool via
:func:`set_pool` at startup (preferred — avoids multiple pools per process);
otherwise one is lazily created from ``DATABASE_URL`` on first use.

If ``DATABASE_URL`` is unset or the DB is unreachable, :func:`get_pool` returns
``None`` and callers fall back to env-only / default behaviour.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_pool: Any | None = None  # asyncpg.Pool, kept untyped to avoid a hard import


def set_pool(pool: Any) -> None:
    """Inject an existing asyncpg pool. Call this from your service startup."""
    global _pool
    _pool = pool


async def get_pool() -> Any | None:
    """Return the shared pool, lazily creating one from ``DATABASE_URL``.

    Returns ``None`` if no URL is configured, asyncpg is missing, or the DB is
    unreachable — callers must treat that as "no DB available".
    """
    global _pool
    if _pool is not None:
        return _pool
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    try:
        import asyncpg  # local import: services without DB don't pay the cost
    except ImportError:
        logger.warning("asyncpg is not installed; DB-backed features disabled")
        return None
    try:
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
        return _pool
    except Exception as exc:  # noqa: BLE001 — DB unreachable is non-fatal
        logger.warning("Could not create DB pool: %s", exc)
        return None

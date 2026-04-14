"""Agent memory – per use_case persistent memory in PostgreSQL."""

from __future__ import annotations

import logging

from config import settings
from core.database import get_pool

logger = logging.getLogger(__name__)

# Fallback in-memory store (used when DB is unavailable)
_memory_store: dict[str, str] = {}


async def read_memory(use_case: str) -> str:
    """Read memory for a use case. Returns empty string if none exists."""
    try:
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT memory_text FROM agent_memory WHERE use_case = $1", use_case
        )
        return row["memory_text"] if row else ""
    except Exception as exc:
        logger.warning(f"DB read failed, using in-memory fallback: {exc}")
        return _memory_store.get(use_case, "")


async def write_memory(use_case: str, memory: str) -> None:
    """Write memory for a use case, truncating to MEMORY_MAX_CHARS."""
    truncated = memory[: settings.MEMORY_MAX_CHARS]
    try:
        pool = await get_pool()
        await pool.execute(
            """INSERT INTO agent_memory (use_case, memory_text, updated_at)
               VALUES ($1, $2, NOW())
               ON CONFLICT (use_case)
               DO UPDATE SET memory_text = $2, version = agent_memory.version + 1, updated_at = NOW()""",
            use_case,
            truncated,
        )
    except Exception as exc:
        logger.warning(f"DB write failed, using in-memory fallback: {exc}")
        _memory_store[use_case] = truncated


def clear_memory_store() -> None:
    """Clear the in-memory store (used in tests)."""
    _memory_store.clear()

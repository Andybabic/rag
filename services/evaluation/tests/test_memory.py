"""Tests for agent memory."""

from __future__ import annotations

import pytest
from core.memory import clear_memory_store, read_memory, write_memory


@pytest.fixture(autouse=True)
def _clean_memory():
    clear_memory_store()
    yield
    clear_memory_store()


@pytest.mark.anyio
async def test_read_empty_memory():
    result = await read_memory("session-1")
    assert result == ""


@pytest.mark.anyio
async def test_write_and_read_memory():
    await write_memory("session-1", "Important context")
    result = await read_memory("session-1")
    assert result == "Important context"


@pytest.mark.anyio
async def test_memory_truncation():
    from config import settings

    long_text = "x" * (settings.MEMORY_MAX_CHARS + 100)
    await write_memory("session-1", long_text)
    result = await read_memory("session-1")
    assert len(result) == settings.MEMORY_MAX_CHARS


@pytest.mark.anyio
async def test_separate_sessions():
    await write_memory("session-a", "Context A")
    await write_memory("session-b", "Context B")
    assert await read_memory("session-a") == "Context A"
    assert await read_memory("session-b") == "Context B"

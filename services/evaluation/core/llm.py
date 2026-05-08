"""LLM chat client.

Thin shim over the central provider pipeline in ``shared.llm``.
The active backend is selected via ``LLM_PROVIDER`` env var.
"""

from __future__ import annotations

from config import settings
from shared.llm import LLMUnavailableError  # re-export for callers
from shared.llm import chat as _chat

__all__ = ["LLMUnavailableError", "call_llm"]


async def call_llm(
    messages: list[dict],
    *,
    model: str | None = None,
) -> str:
    return await _chat(
        messages,
        model=model or settings.LLM_MODEL,
        options={"temperature": 0.2},
    )

"""LLM client for Ollama chat completions."""

from __future__ import annotations

import httpx
from config import settings


class LLMUnavailableError(Exception):
    """Raised when the LLM service is not reachable."""


async def call_llm(
    messages: list[dict],
    *,
    model: str | None = None,
) -> str:
    """Send messages to Ollama and return the assistant response text."""
    model = model or settings.LLM_MODEL
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.2},
                },
            )
            response.raise_for_status()
            return response.json()["message"]["content"]
    except httpx.ConnectError as exc:
        raise LLMUnavailableError(
            f"Ollama not reachable at {settings.OLLAMA_BASE_URL}: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise LLMUnavailableError(
            f"Ollama returned {exc.response.status_code}: {exc.response.text}"
        ) from exc

"""Embedding client.

Thin shim over the central provider pipeline in ``shared.llm``.
Behaviour and public API are unchanged; the actual backend (Ollama,
OpenAI, ...) is selected via ``EMBEDDING_PROVIDER`` env var.
"""

from __future__ import annotations

from config import settings
from shared.llm import OllamaUnavailableError  # re-export for callers
from shared.llm import embed as _embed
from shared.llm import list_models as _list_models

__all__ = [
    "OllamaUnavailableError",
    "embed_batch",
    "embed_text",
    "list_models",
]


async def embed_text(text: str, *, model: str | None = None) -> list[float]:
    return await _embed(text, model=model or settings.EMBEDDING_MODEL)


async def embed_batch(
    texts: list[str],
    *,
    model: str | None = None,
    batch_size: int | None = None,
) -> list[list[float]]:
    size = batch_size or settings.EMBED_BATCH_SIZE
    vectors: list[list[float]] = []
    for i in range(0, len(texts), size):
        for text in texts[i : i + size]:
            vectors.append(await embed_text(text, model=model))
    return vectors


async def list_models() -> list[dict]:
    return await _list_models()

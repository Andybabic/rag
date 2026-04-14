"""Ollama embedding client."""

from __future__ import annotations

import httpx
from config import settings


class OllamaUnavailableError(Exception):
    """Raised when the Ollama service is not reachable."""


async def embed_text(text: str, *, model: str | None = None) -> list[float]:
    """Get embedding vector for a single text from Ollama."""
    model = model or settings.EMBEDDING_MODEL
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/embeddings",
                json={"model": model, "prompt": text},
            )
            response.raise_for_status()
            return response.json()["embedding"]
    except httpx.ConnectError as exc:
        raise OllamaUnavailableError(
            f"Ollama not reachable at {settings.OLLAMA_BASE_URL}: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise OllamaUnavailableError(
            f"Ollama returned {exc.response.status_code}: {exc.response.text}"
        ) from exc


async def embed_batch(
    texts: list[str],
    *,
    model: str | None = None,
    batch_size: int | None = None,
) -> list[list[float]]:
    """Embed multiple texts, splitting into batches of batch_size."""
    size = batch_size or settings.EMBED_BATCH_SIZE
    vectors: list[list[float]] = []
    for i in range(0, len(texts), size):
        batch = texts[i : i + size]
        batch_vectors = []
        for text in batch:
            vec = await embed_text(text, model=model)
            batch_vectors.append(vec)
        vectors.extend(batch_vectors)
    return vectors


async def list_models() -> list[dict]:
    """List available models from Ollama."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            return response.json().get("models", [])
    except httpx.ConnectError as exc:
        raise OllamaUnavailableError(
            f"Ollama not reachable at {settings.OLLAMA_BASE_URL}: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise OllamaUnavailableError(
            f"Ollama returned {exc.response.status_code}: {exc.response.text}"
        ) from exc

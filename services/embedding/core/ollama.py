"""Embedding client.

Thin shim over the central provider pipeline in ``shared.llm``.
The backend (Ollama, OpenAI, ...) is selected globally via the
``EMBEDDING_PROVIDER`` env var, or per use case via the dashboard config
(resolved through :func:`resolve_embedding`).
"""

from __future__ import annotations

import logging
from typing import Any

from config import settings
from shared.llm import OllamaUnavailableError  # re-export for callers
from shared.llm import embed as _embed
from shared.llm import list_models as _list_models

__all__ = [
    "OllamaUnavailableError",
    "embed_batch",
    "embed_text",
    "list_models",
    "resolve_embedding",
]

logger = logging.getLogger(__name__)


async def resolve_embedding(
    use_case: str | None = None,
    model: str | None = None,
) -> tuple[Any | None, str]:
    """Resolve the embedding config + model for a request.

    With a ``use_case`` the provider/model/endpoint come from that use case's
    dashboard config (``shared.usecase_config.resolve_config``), which itself
    falls back to env. Without one — or if the config layer is unavailable
    (missing deps / DB down) — we use the service's env defaults, preserving
    the previous behaviour. ``model`` always wins when given.
    """
    cfg: Any | None = None
    if use_case:
        try:
            from shared.usecase_config import resolve_config

            cfg = await resolve_config(use_case)
        except Exception as exc:  # noqa: BLE001 — degrade to env, never crash
            logger.warning("Per-usecase embedding config unavailable (%s); using env", exc)
            cfg = None
    resolved_model = model or getattr(cfg, "embedding_model", None) or settings.EMBEDDING_MODEL
    return cfg, resolved_model


async def embed_text(
    text: str,
    *,
    model: str | None = None,
    config: Any | None = None,
) -> list[float]:
    return await _embed(text, model=model or settings.EMBEDDING_MODEL, config=config)


async def embed_batch(
    texts: list[str],
    *,
    model: str | None = None,
    batch_size: int | None = None,
    config: Any | None = None,
) -> list[list[float]]:
    size = batch_size or settings.EMBED_BATCH_SIZE
    vectors: list[list[float]] = []
    for i in range(0, len(texts), size):
        for text in texts[i : i + size]:
            vectors.append(await embed_text(text, model=model, config=config))
    return vectors


async def list_models() -> list[dict]:
    return await _list_models()

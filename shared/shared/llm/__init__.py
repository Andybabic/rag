"""Central LLM/embedding provider pipeline.

Single entry point for chat, embeddings, vision and model listing.
The active backend (Ollama, OpenAI, ...) is selected via env vars
``LLM_PROVIDER`` / ``EMBEDDING_PROVIDER`` / ``VISION_PROVIDER``.

To add a new provider:
    1. Subclass ``LLMProvider`` in ``shared.llm.providers``.
    2. Decorate it with ``@register("name")``.
    3. Import it in ``shared/llm/providers/__init__.py``.

Nothing else needs to change — every service goes through ``chat`` /
``embed`` / ``vision_describe`` / ``list_models`` below.
"""

from shared.llm.base import LLMProvider
from shared.llm.config import LLMConfig
from shared.llm.errors import LLMUnavailableError, OllamaUnavailableError
from shared.llm.pipeline import (
    chat,
    embed,
    embed_batch,
    list_models,
    vision_describe,
)
from shared.llm.registry import get_provider, register

__all__ = [
    "LLMConfig",
    "LLMProvider",
    "LLMUnavailableError",
    "OllamaUnavailableError",
    "chat",
    "embed",
    "embed_batch",
    "get_provider",
    "list_models",
    "register",
    "vision_describe",
]

"""Abstract provider interface.

Every backend (Ollama, OpenAI, ...) implements this so the pipeline
can dispatch chat / embed / vision / list_models the same way.

Subclasses only need to override the methods they actually support;
the defaults raise ``NotImplementedError`` with a clear message.
"""

from __future__ import annotations

from abc import ABC

from shared.llm.config import LLMConfig
from shared.llm.errors import LLMUnavailableError


class LLMProvider(ABC):
    """Base class for all LLM/embedding/vision backends."""

    name: str = ""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    async def chat(self, messages: list[dict], *, model: str, **opts) -> str:
        raise NotImplementedError(
            f"Provider {self.name!r} does not support chat completions"
        )

    async def embed(self, text: str, *, model: str) -> list[float]:
        raise NotImplementedError(
            f"Provider {self.name!r} does not support embeddings"
        )

    async def list_models(self) -> list[dict]:
        raise NotImplementedError(
            f"Provider {self.name!r} does not support listing models"
        )

    async def vision_describe(
        self,
        prompt: str,
        image_base64: str,
        *,
        model: str,
    ) -> str:
        raise NotImplementedError(
            f"Provider {self.name!r} does not support vision"
        )

    @staticmethod
    def _wrap_error(label: str, exc: Exception) -> LLMUnavailableError:
        return LLMUnavailableError(f"{label}: {exc}")

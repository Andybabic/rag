"""Configuration for the LLM provider pipeline.

Reads provider selection and credentials from environment variables.
Each role (chat, embedding, vision) can point at a different provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


@dataclass(frozen=True)
class LLMConfig:
    """Snapshot of the LLM-related env vars at call time.

    Roles map to provider names registered via ``@register(...)``.
    Per-provider settings live in their own fields and are read by the
    provider implementations as they need them.
    """

    chat_provider: str = "ollama"
    embedding_provider: str = "ollama"
    vision_provider: str = "ollama"

    ollama_base_url: str = "http://localhost:11434"
    ollama_api_key: str | None = None

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None

    @classmethod
    def from_env(cls) -> LLMConfig:
        return cls(
            chat_provider=(_env("LLM_PROVIDER", "ollama") or "ollama").lower(),
            embedding_provider=(_env("EMBEDDING_PROVIDER", "ollama") or "ollama").lower(),
            vision_provider=(_env("VISION_PROVIDER", "ollama") or "ollama").lower(),
            ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434",
            ollama_api_key=_env("OLLAMA_API_KEY"),
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1",
            openai_api_key=_env("OPENAI_API_KEY"),
        )

    def provider_for(self, role: str) -> str:
        if role == "chat":
            return self.chat_provider
        if role == "embedding":
            return self.embedding_provider
        if role == "vision":
            return self.vision_provider
        raise ValueError(f"Unknown LLM role: {role!r}")

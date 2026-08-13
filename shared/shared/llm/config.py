"""Configuration for the LLM provider pipeline.

Reads provider selection and credentials from environment variables.
Each role (chat, embedding, vision) can point at a different provider.

Per-usecase overrides are applied on top of this snapshot by
``shared.usecase_config.resolve_config``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float | None = None) -> float | None:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMConfig:
    """Snapshot of the LLM-related env vars at call time.

    Roles map to provider names registered via ``@register(...)``.
    Per-provider settings live in their own fields and are read by the
    provider implementations as they need them.

    The model + tuning fields (``llm_model``, ``embedding_model`` etc.)
    are advisory: callers may override them per request. They exist on
    this snapshot so the resolver can deliver per-usecase defaults.
    """

    chat_provider: str = "ollama"
    embedding_provider: str = "ollama"
    vision_provider: str = "ollama"

    ollama_base_url: str = "http://localhost:11434"
    ollama_api_key: str | None = None
    ollama_num_ctx: int = 32768

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None

    # Optional dedicated OpenAI-compatible endpoint + key for embeddings, so
    # embeddings can run on a different server than chat/vision. Falls back to
    # ``openai_base_url`` / ``openai_api_key`` when unset.
    embedding_openai_base_url: str | None = None
    embedding_openai_api_key: str | None = None

    llm_model: str | None = None
    embedding_model: str | None = None
    vision_model: str | None = None
    embedding_dimension: int | None = None

    temperature: float | None = None
    max_tokens: int | None = None
    embed_batch_size: int | None = None
    agent_max_steps: int | None = None
    memory_max_chars: int | None = None

    # Read timeout (seconds) for vision calls. Vision models on slow OpenAI-
    # compatible endpoints can take well over a minute per image; too short a
    # timeout aborts a whole document ingest on one image.
    vision_timeout: float = 180.0

    # Chain-of-thought / thinking tokens (Nemotron, Qwen3, …). Off unless
    # ``THINKING=true`` is set in the environment.
    enable_thinking: bool = False

    @classmethod
    def from_env(cls) -> LLMConfig:
        return cls(
            chat_provider=(_env("LLM_PROVIDER", "ollama") or "ollama").lower(),
            embedding_provider=(_env("EMBEDDING_PROVIDER", "ollama") or "ollama").lower(),
            vision_provider=(_env("VISION_PROVIDER", "ollama") or "ollama").lower(),
            ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434",
            ollama_api_key=_env("OLLAMA_API_KEY"),
            ollama_num_ctx=_env_int("OLLAMA_NUM_CTX", 32768) or 32768,
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1",
            openai_api_key=_env("OPENAI_API_KEY"),
            embedding_openai_base_url=_env("EMBEDDING_OPENAI_BASE_URL"),
            embedding_openai_api_key=_env("EMBEDDING_OPENAI_API_KEY"),
            llm_model=_env("LLM_MODEL"),
            embedding_model=_env("EMBEDDING_MODEL"),
            vision_model=_env("VISION_MODEL"),
            embedding_dimension=_env_int("EMBEDDING_DIMENSION"),
            temperature=_env_float("TEMPERATURE"),
            max_tokens=_env_int("MAX_TOKENS"),
            embed_batch_size=_env_int("EMBED_BATCH_SIZE"),
            agent_max_steps=_env_int("AGENT_MAX_STEPS"),
            memory_max_chars=_env_int("MEMORY_MAX_CHARS"),
            vision_timeout=_env_float("VISION_TIMEOUT", 180.0) or 180.0,
            enable_thinking=_env_bool("THINKING", False),
        )

    def provider_for(self, role: str) -> str:
        if role == "chat":
            return self.chat_provider
        if role == "embedding":
            return self.embedding_provider
        if role == "vision":
            return self.vision_provider
        raise ValueError(f"Unknown LLM role: {role!r}")

    def for_role(self, role: str) -> "LLMConfig":
        """Return a config whose OpenAI base URL / API key are the role-specific
        overrides when set. Currently only embeddings support a dedicated
        endpoint (``EMBEDDING_OPENAI_BASE_URL`` / ``EMBEDDING_OPENAI_API_KEY``);
        other roles keep the shared credentials.
        """
        if role == "embedding" and (
            self.embedding_openai_base_url or self.embedding_openai_api_key
        ):
            return replace(
                self,
                openai_base_url=self.embedding_openai_base_url or self.openai_base_url,
                openai_api_key=self.embedding_openai_api_key or self.openai_api_key,
            )
        return self

"""Public role-based entry points.

Services call these instead of talking to httpx directly. The right
provider is picked via ``LLMConfig.from_env()`` on every call so env
changes take effect without process restarts in tests.

Callers that need per-usecase overrides can pass a pre-resolved
``LLMConfig`` via the ``config=`` keyword (use
``shared.usecase_config.resolve_config(use_case)``).
"""

from __future__ import annotations

from shared.llm.base import LLMProvider
from shared.llm.config import LLMConfig
from shared.llm.registry import get_provider


def _resolve(role: str, config: LLMConfig | None = None) -> LLMProvider:
    cfg = config or LLMConfig.from_env()
    return get_provider(cfg.provider_for(role), cfg)


async def chat(
    messages: list[dict],
    *,
    model: str,
    config: LLMConfig | None = None,
    **opts,
) -> str:
    return await _resolve("chat", config).chat(messages, model=model, **opts)


async def embed(
    text: str,
    *,
    model: str,
    config: LLMConfig | None = None,
) -> list[float]:
    return await _resolve("embedding", config).embed(text, model=model)


async def embed_batch(
    texts: list[str],
    *,
    model: str,
    batch_size: int = 50,
    config: LLMConfig | None = None,
) -> list[list[float]]:
    """Embed many texts. Providers that expose a real batch endpoint can
    override this in the future; for now we sequentialize per text so
    behaviour matches the previous Ollama implementation."""
    provider = _resolve("embedding", config)
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        for text in texts[i : i + batch_size]:
            vectors.append(await provider.embed(text, model=model))
    return vectors


async def list_models(config: LLMConfig | None = None) -> list[dict]:
    return await _resolve("embedding", config).list_models()


async def vision_describe(
    prompt: str,
    image_base64: str,
    *,
    model: str,
    config: LLMConfig | None = None,
) -> str:
    return await _resolve("vision", config).vision_describe(
        prompt, image_base64, model=model
    )

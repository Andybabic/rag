"""Public role-based entry points.

Services call these instead of talking to httpx directly. The right
provider is picked via ``LLMConfig.from_env()`` on every call so env
changes take effect without process restarts in tests.
"""

from __future__ import annotations

from shared.llm.base import LLMProvider
from shared.llm.config import LLMConfig
from shared.llm.registry import get_provider


def _resolve(role: str) -> LLMProvider:
    config = LLMConfig.from_env()
    return get_provider(config.provider_for(role), config)


async def chat(messages: list[dict], *, model: str, **opts) -> str:
    return await _resolve("chat").chat(messages, model=model, **opts)


async def embed(text: str, *, model: str) -> list[float]:
    return await _resolve("embedding").embed(text, model=model)


async def embed_batch(
    texts: list[str],
    *,
    model: str,
    batch_size: int = 50,
) -> list[list[float]]:
    """Embed many texts. Providers that expose a real batch endpoint can
    override this in the future; for now we sequentialize per text so
    behaviour matches the previous Ollama implementation."""
    provider = _resolve("embedding")
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        for text in texts[i : i + batch_size]:
            vectors.append(await provider.embed(text, model=model))
    return vectors


async def list_models() -> list[dict]:
    return await _resolve("embedding").list_models()


async def vision_describe(
    prompt: str,
    image_base64: str,
    *,
    model: str,
) -> str:
    return await _resolve("vision").vision_describe(
        prompt, image_base64, model=model
    )

"""LLM chat client.

Thin shim over the central provider pipeline in ``shared.llm``.
Provider, model, base URL, API key and tuning come from the per-usecase
resolver (``shared.usecase_config``) with ``.env`` as fallback.
"""

from __future__ import annotations

from config import settings
from shared.llm import LLMUnavailableError  # re-export for callers
from shared.llm import chat as _chat
from shared.usecase_config import resolve_config

__all__ = ["LLMUnavailableError", "call_llm"]


async def call_llm(
    messages: list[dict],
    *,
    use_case: str | None = None,
    model: str | None = None,
) -> str:
    cfg = await resolve_config(use_case)
    chosen_model = model or cfg.llm_model or settings.LLM_MODEL
    options: dict = {"temperature": cfg.temperature if cfg.temperature is not None else 0.2}
    if cfg.max_tokens is not None:
        options["max_tokens"] = cfg.max_tokens
    return await _chat(
        messages,
        model=chosen_model,
        config=cfg,
        options=options,
    )

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

__all__ = [
    "LLMUnavailableError",
    "call_llm",
    "TOKENS_PLAN",
    "TOKENS_AGENT_STEP",
    "TOKENS_SYNTHESIS",
    "TOKENS_COMPLIANCE",
]


# Output budgets per call purpose, in tokens.
#
# These are runaway guards, not length targets: every value sits well above
# what the corresponding prompt actually asks for, so normal output is never
# truncated. Without a budget a model that falls into a repetition loop
# generates until the HTTP timeout, which costs the whole request its latency
# for zero content. Prompt-level brevity ("2-4 Sätze") stays the mechanism
# that shapes the answer; these only bound the pathological case.
TOKENS_PLAN = 800
"""Decomposition JSON: 1-3 sub-tasks with role, sub-query and focus."""

TOKENS_AGENT_STEP = 1500
"""One ReAct step: THOUGHT + ACTION JSON. Also covers FINAL_ANSWER on the
single-agent path, where the action argument *is* the user-facing answer."""

TOKENS_SYNTHESIS = 2000
"""The final user-facing answer, incl. citation markers."""

TOKENS_COMPLIANCE = 600
"""Verdict JSON plus rewrite guidance."""


def _effective_max_tokens(configured: int | None, requested: int | None) -> int | None:
    """Combine the per-call budget with an explicitly configured ceiling.

    ``MAX_TOKENS`` (env or per-usecase row) is an operator-set global ceiling,
    so it must never be *raised* by a call-site budget — and a call site that
    needs less than the ceiling must not be forced up to it. The lower of the
    two wins; either being unset falls back to the other.
    """
    if configured is None:
        return requested
    if requested is None:
        return configured
    return min(configured, requested)


async def call_llm(
    messages: list[dict],
    *,
    use_case: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> str:
    cfg = await resolve_config(use_case)
    chosen_model = model or cfg.llm_model or settings.LLM_MODEL
    options: dict = {"temperature": cfg.temperature if cfg.temperature is not None else 0.2}
    budget = _effective_max_tokens(cfg.max_tokens, max_tokens)
    if budget is not None:
        options["max_tokens"] = budget
    return await _chat(
        messages,
        model=chosen_model,
        config=cfg,
        options=options,
    )

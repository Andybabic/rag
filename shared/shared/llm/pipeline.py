"""Public role-based entry points.

Services call these instead of talking to httpx directly. The right
provider is picked via ``LLMConfig.from_env()`` on every call so env
changes take effect without process restarts in tests.

Callers that need per-usecase overrides can pass a pre-resolved
``LLMConfig`` via the ``config=`` keyword (use
``shared.usecase_config.resolve_config(use_case)``).
"""

from __future__ import annotations

import contextvars
import time

from shared.llm.base import LLMProvider
from shared.llm.config import LLMConfig
from shared.llm.registry import get_provider

__all__ = [
    "chat",
    "embed",
    "embed_batch",
    "list_models",
    "vision_describe",
    "get_last_llm_timing",
    "reset_llm_timing",
]

# Per-request timing store using contextvars so concurrent requests don't cross each other's data.
_last_llm_timing: contextvars.ContextVar = contextvars.ContextVar('last_llm_timing', default={})


def get_last_llm_timing() -> dict:
    """Return timing from the most recent Ollama /chat call within this request.

    Keys: ``load_ms``, ``pp_ms``, ``tp_ms``, ``total_ms``,
          ``prompt_tokens``, ``completion_tokens``.
    Returns empty dict when there is no data yet.
    """
    return dict(_last_llm_timing.get())


def reset_llm_timing() -> None:
    """Clear the per-request timing store. Call before a function that may
    or may not make an LLM call, so that ``get_last_llm_timing()`` returns
    ``{}`` instead of stale data from the previous caller."""
    _last_llm_timing.set({})


def _resolve(role: str, config: LLMConfig | None = None) -> LLMProvider:
    cfg = config or LLMConfig.from_env()
    # for_role swaps in role-specific credentials (e.g. a dedicated embedding
    # endpoint) so a single provider class can talk to different servers.
    return get_provider(cfg.provider_for(role), cfg.for_role(role))


def _tracer():
    try:
        from shared.phoenix import get_tracer
        return get_tracer("shared.llm")
    except Exception:
        return None


def _ns_to_ms(counts: dict, key: str) -> float:
    raw = counts.get(key)
    return round(raw / 1_000_000, 1) if raw else 0


def _capture_llm_timing(provider, wall_ms: float | None = None):
    """Store timing from the most recent chat call (Ollama breakdown + wall clock)."""
    counts = getattr(provider, "last_token_counts", {}) or {}
    timing: dict = {}
    if counts:
        timing = {
            "load_ms": _ns_to_ms(counts, "load_duration"),
            "pp_ms": _ns_to_ms(counts, "prompt_eval_duration"),
            "tp_ms": _ns_to_ms(counts, "eval_duration"),
            "total_ms": _ns_to_ms(counts, "total_duration"),
            "prompt_tokens": counts.get("prompt_eval_count", 0) or 0,
            "completion_tokens": counts.get("eval_count", 0) or 0,
        }
    if wall_ms is not None:
        timing["wall_ms"] = wall_ms
        if not timing.get("total_ms"):
            timing["total_ms"] = wall_ms
    if timing:
        _last_llm_timing.set(timing)


def _set_token_counts(span, provider):
    """Extract token counts from provider (e.g. Ollama) and set on span."""
    from openinference.semconv.trace import SpanAttributes

    counts = getattr(provider, "last_token_counts", {})
    if not counts:
        return

    prompt_tokens = counts.get("prompt_eval_count", 0) or 0
    completion_tokens = counts.get("eval_count", 0) or 0
    total_tokens = prompt_tokens + completion_tokens

    if total_tokens > 0:
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, prompt_tokens)
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, completion_tokens)
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_TOTAL, total_tokens)


async def chat(
    messages: list[dict],
    *,
    model: str,
    config: LLMConfig | None = None,
    **opts,
) -> str:
    t0 = time.perf_counter()
    tracer = _tracer()
    if tracer is None:
        provider = _resolve("chat", config)
        result = await provider.chat(messages, model=model, **opts)
        _capture_llm_timing(provider, wall_ms=round((time.perf_counter() - t0) * 1000, 1))
        return result

    with tracer.start_as_current_span("llm.chat") as span:
        from openinference.semconv.trace import (
            SpanAttributes,
            OpenInferenceSpanKindValues,
        )

        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.LLM.value)
        span.set_attribute(SpanAttributes.LLM_MODEL_NAME, model)
        span.set_attribute(SpanAttributes.LLM_INPUT_MESSAGES, str(messages))

        try:
            provider = _resolve("chat", config)
            result = await provider.chat(messages, model=model, **opts)
            _capture_llm_timing(provider, wall_ms=round((time.perf_counter() - t0) * 1000, 1))
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, result)
            _set_token_counts(span, provider)
            return result
        except Exception as exc:
            span.record_exception(exc)
            raise


async def embed(
    text: str,
    *,
    model: str,
    config: LLMConfig | None = None,
) -> list[float]:
    tracer = _tracer()
    if tracer is None:
        return await _resolve("embedding", config).embed(text, model=model)

    with tracer.start_as_current_span("llm.embed") as span:
        from openinference.semconv.trace import (
            SpanAttributes,
            OpenInferenceSpanKindValues,
        )

        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.EMBEDDING.value)
        span.set_attribute(SpanAttributes.EMBEDDING_MODEL_NAME, model)
        span.set_attribute(SpanAttributes.INPUT_VALUE, text)

        try:
            result = await _resolve("embedding", config).embed(text, model=model)
            span.set_attribute(SpanAttributes.EMBEDDING_EMBEDDINGS, str([{"vector_dim": len(result)}]))
            return result
        except Exception as exc:
            span.record_exception(exc)
            raise


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
    tracer = _tracer()
    if tracer is None:
        return await _resolve("vision", config).vision_describe(
            prompt, image_base64, model=model
        )

    with tracer.start_as_current_span("llm.vision") as span:
        from openinference.semconv.trace import (
            SpanAttributes,
            OpenInferenceSpanKindValues,
        )

        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.LLM.value)
        span.set_attribute(SpanAttributes.LLM_MODEL_NAME, model)
        span.set_attribute(SpanAttributes.INPUT_VALUE, prompt)

        try:
            provider = _resolve("vision", config)
            result = await provider.vision_describe(
                prompt, image_base64, model=model
            )
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, result)
            _set_token_counts(span, provider)
            return result
        except Exception as exc:
            span.record_exception(exc)
            raise

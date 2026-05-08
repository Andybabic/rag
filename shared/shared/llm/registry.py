"""Provider registry — single extension point.

Adding a new backend = subclass ``LLMProvider`` and decorate with
``@register("name")``. The pipeline will pick it up by env var.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from shared.llm.base import LLMProvider
from shared.llm.config import LLMConfig

T = TypeVar("T", bound=LLMProvider)

_REGISTRY: dict[str, type[LLMProvider]] = {}


def register(name: str) -> Callable[[type[T]], type[T]]:
    """Class decorator: register a provider under ``name``."""

    def decorator(cls: type[T]) -> type[T]:
        cls.name = name
        _REGISTRY[name.lower()] = cls
        return cls

    return decorator


def get_provider(name: str, config: LLMConfig | None = None) -> LLMProvider:
    """Instantiate the provider registered under ``name``."""
    # Trigger provider module imports so decorators run.
    from shared.llm import providers  # noqa: F401

    key = name.lower()
    if key not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise ValueError(
            f"Unknown LLM provider: {name!r}. Registered providers: {available}"
        )
    return _REGISTRY[key](config or LLMConfig.from_env())


def registered_providers() -> list[str]:
    return sorted(_REGISTRY)

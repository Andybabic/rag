"""Provider-agnostic exceptions."""

from __future__ import annotations


class LLMUnavailableError(Exception):
    """Raised when the configured LLM/embedding backend is not reachable
    or returned a non-success status."""


# Backwards-compatible alias for code paths that still expect the old name.
OllamaUnavailableError = LLMUnavailableError

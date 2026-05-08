"""Built-in providers. Imports trigger ``@register(...)`` decorators."""

from shared.llm.providers import ollama as _ollama  # noqa: F401
from shared.llm.providers import openai as _openai  # noqa: F401

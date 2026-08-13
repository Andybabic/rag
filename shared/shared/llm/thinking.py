"""Strip chain-of-thought / thinking blocks from assistant messages.

Thinking models (Nemotron, Qwen3, DeepSeek-R1, …) mix reasoning and the
actual reply. Reasoning may live:

- inside ``content`` between ``<think>…</think>`` (or only a closing tag)
- in ``reasoning`` / ``reasoning_content`` / ``thinking`` on the message

Callers always want the visible reply so agent parsing and the UI stay
stable.
"""

from __future__ import annotations

import re
from typing import Any

_CLOSED_THINK = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>",
    re.DOTALL | re.IGNORECASE,
)
_CLOSE_TAG = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Return the assistant reply with thinking blocks removed."""
    if not text:
        return ""
    out = _CLOSED_THINK.sub("", text)
    if _CLOSE_TAG.search(out):
        tail = _CLOSE_TAG.split(out)[-1]
        out = tail if tail.strip() else _CLOSE_TAG.sub("", out)
    return out.strip()


def _coerce_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def openai_thinking_kwargs(enabled: bool) -> dict:
    """vLLM / NIM / SGLang: disable CoT unless ``THINKING=true``.

    Sent as ``chat_template_kwargs.enable_thinking`` on chat-completions.
    """
    return {"chat_template_kwargs": {"enable_thinking": bool(enabled)}}


def extract_assistant_text(message: dict | None) -> str:
    """Pull the user-facing reply out of a chat-completions ``message`` object."""
    if not message:
        return ""
    content = strip_thinking(_coerce_content(message.get("content")))
    if content:
        return content
    for key in ("reasoning_content", "reasoning", "thinking"):
        extra = message.get(key)
        if isinstance(extra, str) and extra.strip():
            stripped = strip_thinking(extra)
            if stripped:
                return stripped
    return ""

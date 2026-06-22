"""Helpers for passing images through chat messages.

A chat message may carry images via an ``"images"`` key holding a list of
either raw base64 strings or full data URIs (``data:image/png;base64,...``).
Providers normalise these to their own wire format:

- Ollama wants raw base64 (no prefix) in the message's ``images`` array.
- OpenAI wants a ``content`` array with ``image_url`` parts (data URIs).
"""

from __future__ import annotations

import re

_DATA_URI_RE = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.*)$", re.DOTALL)


def to_raw_base64(image: str) -> str:
    """Return the bare base64 payload, stripping any ``data:...;base64,`` prefix."""
    match = _DATA_URI_RE.match(image.strip())
    return match.group("data") if match else image.strip()


def to_data_uri(image: str, *, default_mime: str = "image/png") -> str:
    """Return a ``data:<mime>;base64,...`` URI, wrapping raw base64 if needed."""
    image = image.strip()
    if _DATA_URI_RE.match(image):
        return image
    return f"data:{default_mime};base64,{image}"

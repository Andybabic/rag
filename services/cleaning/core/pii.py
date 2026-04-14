"""Regex-based PII removal for Austrian context."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")),
    (
        "PHONE",
        re.compile(
            r"(?:\+43|0043|0)"       # Austrian prefix
            r"[\s\-/]?"
            r"\d{1,4}"              # area / mobile block
            r"[\s\-/]?"
            r"\d{3,10}"             # subscriber number
        ),
    ),
    ("IP", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
]


def remove_pii(text: str) -> str:
    """Replace known PII patterns with [TYPE_REMOVED] placeholders."""
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[{label}_REMOVED]", text)
    return text

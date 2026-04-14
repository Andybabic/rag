"""Core cleaning logic – delegates to the appropriate file handler."""

from __future__ import annotations

import os

from models import ParsedDocument

from core.handlers import HANDLER_REGISTRY, supported_formats


async def clean(data: bytes, filename: str) -> ParsedDocument:
    """Extract structured content from raw file bytes based on file extension."""
    ext = os.path.splitext(filename)[1].lower()
    handler = HANDLER_REGISTRY.get(ext)
    if handler is None:
        supported = ", ".join(supported_formats())
        raise ValueError(f"Unsupported file format: {ext!r}. Supported formats: {supported}")
    return await handler.parse(data, filename)

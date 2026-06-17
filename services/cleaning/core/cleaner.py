"""Core cleaning logic – delegates to the appropriate file handler."""

from __future__ import annotations

import os

from models import ParsedDocument

from core.handlers import (
    HANDLER_REGISTRY,
    GCodeHandler,
    decode_cnc_bytes,
    looks_like_cnc,
    supported_formats,
)

_GCODE_HANDLER = GCodeHandler()


async def clean(data: bytes, filename: str) -> ParsedDocument:
    """Extract structured content from raw file bytes.

    Dispatch primär über die Dateiendung. Hat eine Datei KEINE bekannte
    Endung (typisch für Sinumerik/MPF-Maschinendateien, die ohne Endung
    aus der Maschine kommen), wird per Content-Sniffing geprüft, ob es
    CNC-Code ist – bevor das Format abgelehnt wird.
    """
    ext = os.path.splitext(filename)[1].lower()
    handler = HANDLER_REGISTRY.get(ext)
    if handler is not None:
        return await handler.parse(data, filename)

    # Endungslos / unbekannt: CNC-Code am Inhalt erkennen.
    if looks_like_cnc(decode_cnc_bytes(data)):
        return await _GCODE_HANDLER.parse(data, filename)

    supported = ", ".join(supported_formats())
    raise ValueError(f"Unsupported file format: {ext!r}. Supported formats: {supported}")

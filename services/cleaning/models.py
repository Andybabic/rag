from dataclasses import dataclass, field

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


@dataclass
class ParsedDocument:
    text: str
    pages: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class MaxRetriesExceeded(Exception):
    """Raised when all retry attempts for a MineU call have been exhausted."""


class DegenerateExtractionError(Exception):
    """Raised when a PDF yielded (almost) no extractable text.

    Typically a scanned / image-based PDF parsed without OCR. Failing
    loudly here prevents a silent near-empty ingest (the failure mode that
    made an entire document set unsearchable without any error)."""

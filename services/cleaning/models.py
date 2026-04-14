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

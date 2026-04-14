"""Shared Pydantic models used across all RAG platform services."""

from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ChunkMetadata(BaseModel):
    chunk_id: str = Field(default_factory=lambda: str(uuid4()))
    file_name: str
    page: Optional[int] = None
    total_pages: Optional[int] = None
    doc_type: str  # "pdf", "docx", "csv", "txt", "image"
    use_case: str  # "neumann", "gw_stpoelten", "wiener_linien"
    collection: str  # Ziel-Collection in Qdrant
    extra: dict = Field(default_factory=dict)


class Chunk(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    text: str
    metadata: ChunkMetadata


class EmbeddedChunk(BaseModel):
    chunk: Chunk
    vector: list[float]
    model: str
    dimension: int


class SearchResult(BaseModel):
    chunk: Chunk
    score: float


class APIError(BaseModel):
    error: str
    detail: str
    request_id: str
    service: str

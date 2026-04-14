from __future__ import annotations

from pydantic import BaseModel, Field


class EmbedRequest(BaseModel):
    type: str = "text"
    content: str
    metadata: dict = Field(default_factory=dict)


class EmbedResponse(BaseModel):
    vector: list[float]
    metadata: dict
    model: str
    dimension: int
    request_id: str


class BatchChunk(BaseModel):
    type: str = "text"
    content: str
    metadata: dict = Field(default_factory=dict)


class BatchEmbedRequest(BaseModel):
    chunks: list[BatchChunk]
    batch_size: int | None = None


class EmbeddingResult(BaseModel):
    chunk_id: str
    vector: list[float]
    model: str
    dimension: int


class BatchEmbedResponse(BaseModel):
    embeddings: list[EmbeddingResult]
    total: int
    failed: int
    errors: list[str]


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str

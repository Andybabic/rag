from __future__ import annotations

from pydantic import BaseModel, Field


class EmbedRequest(BaseModel):
    type: str = "text"
    content: str
    metadata: dict = Field(default_factory=dict)
    # Optional per-usecase resolution: when a use_case is given, the embedding
    # provider/model/endpoint are resolved from its dashboard config (falling
    # back to env). ``model`` explicitly overrides the resolved model.
    use_case: str | None = None
    model: str | None = None


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
    # See EmbedRequest: per-usecase provider/model resolution for ingestion.
    use_case: str | None = None
    model: str | None = None


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

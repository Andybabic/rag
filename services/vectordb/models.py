from __future__ import annotations

from pydantic import BaseModel, Field


class EmbeddingPayload(BaseModel):
    chunk_id: str
    vector: list[float]
    metadata: dict = Field(default_factory=dict)


class UpsertRequest(BaseModel):
    collection: str
    embeddings: list[EmbeddingPayload]
    # Embedding model that produced these vectors. Recorded on the index so
    # a later query with a different model can be detected instead of
    # silently returning meaningless (near-zero-similarity) results.
    embed_model: str | None = None


class SearchRequest(BaseModel):
    collection: str
    vector: list[float]
    top_k: int = 5
    filters: dict = Field(default_factory=dict)
    # Model used to embed this query. If it diverges from the model the
    # collection was built with, the search is rejected with a clear error.
    embed_model: str | None = None


class DeletePointsRequest(BaseModel):
    collection: str
    filters: dict = Field(default_factory=dict)


class ScrollRequest(BaseModel):
    collection: str
    filters: dict = Field(default_factory=dict)
    # Up to 1000: replacing a product reads every point's file_hash first.
    limit: int = Field(default=1, ge=1, le=1000)


class CrossSearchRequest(BaseModel):
    primary_collection: str
    linked_collections: list[str]
    vector: list[float]
    link_key: str
    top_k: int = 5
    filters: dict = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class ExportPointsRequest(BaseModel):
    collection: str
    # Offset from the previous page's ``next``; point ids are UUIDs or ints.
    offset: str | int | None = None
    limit: int = Field(default=256, ge=1, le=1000)


class ImportPoint(BaseModel):
    id: str | int
    vector: list[float]
    payload: dict = Field(default_factory=dict)


class ImportPointsRequest(BaseModel):
    collection: str
    dimension: int = Field(ge=1)
    points: list[ImportPoint] = Field(default_factory=list)
    # First page: create the collection, and refuse if it already exists.
    create: bool = False

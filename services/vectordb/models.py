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

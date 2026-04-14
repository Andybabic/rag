from __future__ import annotations

from pydantic import BaseModel, Field


class EmbeddingPayload(BaseModel):
    chunk_id: str
    vector: list[float]
    metadata: dict = Field(default_factory=dict)


class UpsertRequest(BaseModel):
    collection: str
    embeddings: list[EmbeddingPayload]


class SearchRequest(BaseModel):
    collection: str
    vector: list[float]
    top_k: int = 5
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

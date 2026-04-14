from __future__ import annotations

from pydantic import BaseModel, Field


class StructureConfig(BaseModel):
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    target_collection: str | None = None
    extra: dict = Field(default_factory=dict)


class StructureRequest(BaseModel):
    markdown: str
    metadata: dict = Field(default_factory=dict)
    use_case: str
    config: StructureConfig = Field(default_factory=StructureConfig)


class CNCStructureRequest(BaseModel):
    markdown: str
    metadata: dict = Field(default_factory=dict)
    config: StructureConfig = Field(default_factory=StructureConfig)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str

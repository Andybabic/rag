from __future__ import annotations

from pydantic import BaseModel, Field


class ChunkInput(BaseModel):
    chunk_id: str
    text: str
    score: float
    metadata: dict = Field(default_factory=dict)


class RerankWeights(BaseModel):
    material_similarity: float = 0.4
    param_similarity: float = 0.4
    usage_count: float = 0.2


class RerankConfig(BaseModel):
    threshold: float = 0.3
    weights: RerankWeights = Field(default_factory=RerankWeights)


class RerankRequest(BaseModel):
    query: str
    chunks: list[ChunkInput]
    use_case: str
    config: RerankConfig = Field(default_factory=RerankConfig)


class EvaluateRequest(BaseModel):
    query: str
    chunks: list[ChunkInput]
    use_case: str
    min_chunks: int = 2
    min_score: float = 0.5


class CitationChunk(BaseModel):
    chunk_id: str
    text: str
    metadata: dict = Field(default_factory=dict)


class CitationsRequest(BaseModel):
    answer: str
    chunks: list[CitationChunk]


class AgentConfig(BaseModel):
    collection: str = ""
    filters: dict = Field(default_factory=dict)
    max_steps: int = 3


class HistoryMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class AgentQueryRequest(BaseModel):
    query: str
    use_case: str
    session_id: str
    role: str = "default"
    config: AgentConfig = Field(default_factory=AgentConfig)
    history: list[HistoryMessage] = Field(default_factory=list)
    # User-attached images (data URIs or raw base64), processed by the
    # vision-capable chat model in the same prompt as the query.
    images: list[str] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    query_id: str
    feedback: str
    comment: str = ""


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str

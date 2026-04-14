"""
Interface Agreement – Kermit RAG Platform
==========================================

Zentrale Definition aller Service-Schnittstellen als Pydantic-Modelle.
Basiert auf der vereinheitlichten Analyse aller sechs Use Cases
(RHP, FILL, ESECO, Wiener Linien, GW St. Pölten, Neumann).

Die vier Phasen der Pipeline:
  Phase 1 – Ingestion & Aufbereitung
  Phase 2 – Anfrage & Query-Generierung
  Phase 3 – Ranking & Agent-Loop
  Phase 4 – Generierung & Ausgabe

Service-Übersicht:
  Cleaning Service    (8001) – Dokumentenparsing, Textextraktion
  Data Structure      (8002) – Chunking, Metadaten-Anreicherung via Plugins
  Embedding Service   (8003) – Vektorisierung via Ollama
  VectorDB Service    (8004) – Speicherung und Suche in Qdrant
  Evaluation Service  (8005) – ReAct-Agent, Reranking, Antwortgenerierung
  Frontend Service    (3000) – UI, Pipeline-Orchestrierung, Admin
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UseCase(str, Enum):
    """Registrierte Use Cases mit zugehörigen Plugin-Konfigurationen."""

    NEUMANN = "neumann"
    GW_STPOELTEN = "gw_stpoelten"
    WIENER_LINIEN = "wiener_linien"
    RHP = "rhp"
    FILL = "fill"
    ESECO = "eseco"


class DocType(str, Enum):
    """Unterstützte Dokumenttypen im Cleaning Service."""

    PDF = "pdf"
    DOCX = "docx"
    CSV = "csv"
    TXT = "txt"
    IMAGE = "image"


class AgentAction(str, Enum):
    """Verfügbare Aktionen im ReAct-Agent (Evaluation Service)."""

    SEARCH = "SEARCH"
    SEARCH_CNC = "SEARCH_CNC"
    LOOKUP_SOURCES = "LOOKUP_SOURCES"
    RECALL_MEMORY = "RECALL_MEMORY"
    CLARIFY = "CLARIFY"
    FINAL_ANSWER = "FINAL_ANSWER"


class FeedbackType(str, Enum):
    """Typen von Nutzerfeedback."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


# ---------------------------------------------------------------------------
# Shared / Gemeinsame Modelle
# ---------------------------------------------------------------------------


class ChunkMetadata(BaseModel):
    """Metadaten eines Chunks – gemeinsamer Vertrag zwischen allen Services."""

    chunk_id: str = Field(default_factory=lambda: str(uuid4()))
    file_name: str
    page: Optional[int] = None
    total_pages: Optional[int] = None
    doc_type: str  # DocType-Wert
    use_case: str  # UseCase-Wert
    collection: str  # Ziel-Collection in Qdrant
    extra: dict = Field(
        default_factory=dict,
        description="Use-Case-spezifische Metadaten, angereichert via Plugin",
    )


class Chunk(BaseModel):
    """Ein Text-Chunk mit zugehörigen Metadaten."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    text: str
    metadata: ChunkMetadata


class EmbeddedChunk(BaseModel):
    """Ein Chunk mit seinem Embedding-Vektor."""

    chunk: Chunk
    vector: list[float]
    model: str
    dimension: int


class SearchResult(BaseModel):
    """Ein Suchergebnis aus der VectorDB."""

    chunk: Chunk
    score: float


class APIError(BaseModel):
    """Einheitliches Fehlerformat für alle Services.

    HTTP-Status-Mapping:
      ValueError       → 400
      FileNotFoundError → 404
      PermissionError  → 403
      sonstige         → 500
    """

    error: str
    detail: str
    request_id: str
    service: str


class HealthResponse(BaseModel):
    """Standard-Health-Check-Antwort (GET /health)."""

    status: str
    service: str
    version: str


# ===================================================================
# PHASE 1 – Ingestion & Aufbereitung
# ===================================================================
#
# Pipeline:  Datenquelle → Cleaning → Data Structure → Embedding → VectorDB
# Orchestriert vom Frontend Service (POST /api/ingest)


# ---------------------------------------------------------------------------
# 1.1  Cleaning Service (Port 8001)
#      Frontend → POST /v1/clean
#      Frontend → POST /v1/clean/batch
# ---------------------------------------------------------------------------


class CleanConfig(BaseModel):
    """Optionale Konfiguration für den Cleaning Service.

    Use-Case-spezifisch:
      FILL:    pii_removal=True (Anonymisierung von Kundendaten)
      Neumann: extract_images=True (für LLM-Vision-Captions)
    """

    extract_images: bool = True
    pii_removal: bool = False


class CleanResponse(BaseModel):
    """Antwort von POST /v1/clean – ein einzelnes Dokument."""

    status: str = "ok"
    request_id: str
    markdown: str
    images: list[dict] = Field(
        default_factory=list,
        description="Extrahierte Bilder (Base64 + Metadaten)",
    )
    pages: list[dict] = Field(
        default_factory=list,
        description="Seitenweise Aufteilung des Dokuments",
    )
    metadata: CleanDocumentMetadata


class CleanDocumentMetadata(BaseModel):
    """Metadaten, die der Cleaning Service zu einem Dokument liefert."""

    file_name: str
    total_pages: int
    doc_type: str  # DocType-Wert
    used_mineru: bool = False


class CleanBatchResultItem(BaseModel):
    """Ergebnis für ein einzelnes Dokument im Batch."""

    file_name: str
    status: str
    markdown: str
    metadata: dict


class CleanBatchErrorItem(BaseModel):
    """Fehler für ein einzelnes Dokument im Batch."""

    file_name: str
    error: str
    detail: str


class CleanBatchResponse(BaseModel):
    """Antwort von POST /v1/clean/batch – mehrere Dokumente parallel."""

    status: str = "ok"
    request_id: str
    total: int
    success: int
    failed: int
    results: list[CleanBatchResultItem]
    errors: list[CleanBatchErrorItem]


# ---------------------------------------------------------------------------
# 1.2  Data Structure Service (Port 8002)
#      Frontend → POST /v1/structure
#      Frontend → POST /v1/structure/cnc  (nur GW St. Pölten)
#      GET /v1/use-cases
# ---------------------------------------------------------------------------


class StructureConfig(BaseModel):
    """Konfiguration für Chunking und Collection-Routing."""

    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    target_collection: Optional[str] = None
    extra: dict = Field(
        default_factory=dict,
        description="Use-Case-spezifische Parameter (z.B. product_id)",
    )


class StructureRequest(BaseModel):
    """Request für POST /v1/structure.

    Eingabe: Markdown + Metadaten aus dem Cleaning Service.
    Ausgabe: Chunks mit angereicherter ChunkMetadata + Collection-Routing.

    Das Plugin-System reichert Metadaten Use-Case-spezifisch an:
      Neumann:       machine_id, area (hydraulik/elektrik/mechanik), topic
      GW St. Pölten: cnc_step_id, operation_type, material_class
      Wiener Linien: fahrzeug_typ, kategorie
    """

    markdown: str
    metadata: dict = Field(default_factory=dict)
    use_case: str  # UseCase-Wert
    config: StructureConfig = Field(default_factory=StructureConfig)


class StructureResponse(BaseModel):
    """Antwort von POST /v1/structure."""

    status: str = "ok"
    request_id: str
    use_case: str
    total_chunks: int
    routing: StructureRouting
    chunks: list[Chunk]


class StructureRouting(BaseModel):
    """Routing-Information – in welche Qdrant-Collection die Chunks gehören."""

    collection: str


class CNCStructureRequest(BaseModel):
    """Request für POST /v1/structure/cnc (nur GW St. Pölten).

    Spezialisiertes Chunking für CNC-Programme mit separaten
    Rüstungs- und Materialinfo-Chunks.
    """

    markdown: str
    metadata: dict = Field(
        default_factory=dict,
        description="Enthält product_id, ruest_map_id",
    )
    config: StructureConfig = Field(default_factory=StructureConfig)


class CNCBlock(BaseModel):
    """Ein einzelner CNC-Bearbeitungsschritt."""

    cnc_step_id: str
    operation_type: str
    cutting_speed: float
    tool_type: str
    material_class: str
    text: str
    metadata: dict


class CNCStructureResponse(BaseModel):
    """Antwort von POST /v1/structure/cnc."""

    status: str = "ok"
    request_id: str
    cnc_blocks: list[CNCBlock]
    ruest_chunks: list[Chunk]
    material_chunks: list[Chunk]


class UseCaseInfo(BaseModel):
    """Info zu einem registrierten Use Case (GET /v1/use-cases)."""

    default_collection: str
    collections: list[str]
    description: str
    agent_actions: list[str]


# ---------------------------------------------------------------------------
# 1.3  Embedding Service (Port 8003)
#      Frontend / Evaluation → POST /v1/embed
#      Frontend → POST /v1/embed/batch
#
#      Intern: Ollama (/api/embeddings), Modell: qwen3-embedding:0.6b
# ---------------------------------------------------------------------------


class EmbedRequest(BaseModel):
    """Request für POST /v1/embed – einzelner Text."""

    type: str = "text"
    content: str
    metadata: dict = Field(default_factory=dict)


class EmbedResponse(BaseModel):
    """Antwort von POST /v1/embed."""

    vector: list[float]
    metadata: dict
    model: str
    dimension: int
    request_id: str


class BatchChunk(BaseModel):
    """Ein einzelner Chunk im Batch-Embedding-Request."""

    type: str = "text"
    content: str
    metadata: dict = Field(default_factory=dict)


class BatchEmbedRequest(BaseModel):
    """Request für POST /v1/embed/batch.

    Chunks werden intern in Batches à batch_size (Default: 50) aufgeteilt.
    """

    chunks: list[BatchChunk]
    batch_size: Optional[int] = None


class EmbeddingResult(BaseModel):
    """Ergebnis eines einzelnen Embeddings im Batch."""

    chunk_id: str
    vector: list[float]
    model: str
    dimension: int


class BatchEmbedResponse(BaseModel):
    """Antwort von POST /v1/embed/batch."""

    embeddings: list[EmbeddingResult]
    total: int
    failed: int
    errors: list[str]
    request_id: str


# ---------------------------------------------------------------------------
# 1.4  VectorDB Service (Port 8004)
#      Frontend → POST /v1/upsert
#      GET /v1/collections
#      DELETE /v1/collection/{name}
#
#      Intern: Qdrant (Cosine-Distanz, Auto-Create Collections)
# ---------------------------------------------------------------------------


class EmbeddingPayload(BaseModel):
    """Ein Embedding-Punkt für den Upsert in Qdrant."""

    chunk_id: str
    vector: list[float]
    metadata: dict = Field(default_factory=dict)


class UpsertRequest(BaseModel):
    """Request für POST /v1/upsert.

    Collections werden automatisch erstellt, falls nicht vorhanden.
    chunk_id wird intern via uuid5 zu einer Qdrant-kompatiblen UUID gehasht.
    """

    collection: str
    embeddings: list[EmbeddingPayload]


class UpsertResponse(BaseModel):
    """Antwort von POST /v1/upsert."""

    status: str = "ok"
    upserted: int
    collection: str
    request_id: str


# ===================================================================
# PHASE 2 – Anfrage & Query-Generierung
# ===================================================================
#
# User Query → Evaluation Service (Agent) → Embedding + VectorDB
# Der ReAct-Agent entscheidet über Aktionen (SEARCH, SEARCH_CNC, etc.)


# ---------------------------------------------------------------------------
# 2.1  Evaluation Service – Agent-Query (Port 8005)
#      Frontend → POST /v1/agent/query
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Konfiguration des ReAct-Agenten.

    Use-Case-spezifisch:
      Wiener Linien:  role="trainee" für vereinfachte Sprache
      GW St. Pölten:  Agent nutzt SEARCH_CNC für Cross-Collection-Suche
      FILL:           filters mit Kunde/Anlagentyp
    """

    collection: str = ""
    filters: dict = Field(default_factory=dict)
    max_steps: int = 3


class HistoryMessage(BaseModel):
    """Eine Nachricht im Chatverlauf."""

    role: Literal["user", "assistant"]
    content: str


class AgentQueryRequest(BaseModel):
    """Request für POST /v1/agent/query.

    Zentraler Einstiegspunkt für alle Benutzeranfragen.
    Der Agent orchestriert intern Embedding, VectorDB-Suche,
    Reranking und LLM-Generierung.
    """

    query: str
    use_case: str  # UseCase-Wert
    session_id: str = Field(description="UUID für Sitzungskontext")
    role: str = "default"
    config: AgentConfig = Field(default_factory=AgentConfig)
    history: list[HistoryMessage] = Field(default_factory=list)


class AgentStep(BaseModel):
    """Ein einzelner Schritt des ReAct-Agenten."""

    step: int
    action: str  # AgentAction-Wert
    thought: Optional[str] = None
    observation: Optional[str] = None
    args: Optional[dict] = None


class AgentQueryResponse(BaseModel):
    """Antwort von POST /v1/agent/query."""

    answer: str
    sufficient: bool = Field(
        description="Ob genügend Kontext für eine fundierte Antwort vorhanden war"
    )
    agent_steps: list[AgentStep]
    request_id: str


# ---------------------------------------------------------------------------
# 2.2  VectorDB Service – Suche (Port 8004)
#      Evaluation → POST /v1/search
#      Evaluation → POST /v1/search/cross  (GW St. Pölten)
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    """Request für POST /v1/search – Semantische Suche in einer Collection."""

    collection: str
    vector: list[float]
    top_k: int = 5
    filters: dict = Field(
        default_factory=dict,
        description="Feld:Wert-Paare für Qdrant FieldCondition",
    )


class SearchResultItem(BaseModel):
    """Ein einzelnes Suchergebnis."""

    chunk_id: str
    score: float
    text: str
    metadata: dict


class SearchResponse(BaseModel):
    """Antwort von POST /v1/search."""

    results: list[SearchResultItem]
    total: int
    collection: str
    request_id: str


class CrossSearchRequest(BaseModel):
    """Request für POST /v1/search/cross – Cross-Collection-Suche.

    Sucht zuerst in der primary_collection, dann für jeden Treffer
    in den linked_collections über den link_key (z.B. cnc_step_id).

    Verwendung: GW St. Pölten
      primary: gw_cnc_steps
      linked:  [gw_ruest_data, gw_material_info]
      link_key: cnc_step_id
    """

    primary_collection: str
    linked_collections: list[str]
    vector: list[float]
    link_key: str
    top_k: int = 5
    filters: dict = Field(default_factory=dict)


class CrossSearchResultItem(BaseModel):
    """Ein Ergebnis der Cross-Collection-Suche."""

    primary: SearchResultItem
    linked: dict[str, Optional[SearchResultItem]] = Field(
        description="Collection-Name → verknüpftes Suchergebnis (oder None)"
    )


class CrossSearchResponse(BaseModel):
    """Antwort von POST /v1/search/cross."""

    results: list[CrossSearchResultItem]
    total: int
    request_id: str


# ===================================================================
# PHASE 3 – Ranking & Agent-Loop
# ===================================================================
#
# Läuft innerhalb des Evaluation Service:
# Suchergebnisse → Reranking → Evaluation → ggf. Retry → Kontext bereit


# ---------------------------------------------------------------------------
# 3.1  Reranking (Evaluation Service intern + POST /v1/rerank)
# ---------------------------------------------------------------------------


class ChunkInput(BaseModel):
    """Ein Chunk als Eingabe für Reranking/Evaluation."""

    chunk_id: str
    text: str
    score: float
    metadata: dict = Field(default_factory=dict)


class RerankWeights(BaseModel):
    """Gewichtung der Reranking-Kriterien."""

    material_similarity: float = 0.4
    param_similarity: float = 0.4
    usage_count: float = 0.2


class RerankConfig(BaseModel):
    """Konfiguration für das Reranking."""

    threshold: float = 0.3
    weights: RerankWeights = Field(default_factory=RerankWeights)


class RerankRequest(BaseModel):
    """Request für POST /v1/rerank."""

    query: str
    chunks: list[ChunkInput]
    use_case: str  # UseCase-Wert
    config: RerankConfig = Field(default_factory=RerankConfig)


class RerankResponse(BaseModel):
    """Antwort von POST /v1/rerank."""

    reranked: list[ChunkInput] = Field(description="Chunks über Threshold, sortiert")
    threshold_passed: bool
    below_threshold: list[ChunkInput]
    total: int
    request_id: str


# ---------------------------------------------------------------------------
# 3.2  Evaluation (Evaluation Service intern + POST /v1/evaluate)
# ---------------------------------------------------------------------------


class EvaluateRequest(BaseModel):
    """Request für POST /v1/evaluate.

    Prüft, ob die gefundenen Chunks ausreichen für eine Antwort.
    Falls nein, startet der Agent einen neuen Suchschritt (bis max_steps).
    """

    query: str
    chunks: list[ChunkInput]
    use_case: str  # UseCase-Wert
    min_chunks: int = 2
    min_score: float = 0.5


class EvaluateResponse(BaseModel):
    """Antwort von POST /v1/evaluate."""

    result: bool = Field(description="True wenn Qualität ausreichend")
    reasoning: str = Field(description="Begründung der Entscheidung")
    request_id: str


# ===================================================================
# PHASE 4 – Generierung & Ausgabe
# ===================================================================
#
# Kontext → LLM-Generierung (Ollama, qwen2.5:14b) → Citations → Frontend → Feedback


# ---------------------------------------------------------------------------
# 4.1  Citations (Evaluation Service – POST /v1/citations)
# ---------------------------------------------------------------------------


class CitationChunk(BaseModel):
    """Ein Quell-Chunk für die Quellenzuordnung."""

    chunk_id: str
    text: str
    metadata: dict = Field(default_factory=dict)


class CitationsRequest(BaseModel):
    """Request für POST /v1/citations.

    Ordnet Sätze der generierten Antwort den Quell-Chunks zu.
    """

    answer: str
    chunks: list[CitationChunk]


class CitationMapping(BaseModel):
    """Zuordnung eines Satzes zu seinen Quellen."""

    sentence: str
    sources: list[str] = Field(description="Liste von chunk_ids")


class CitationsResponse(BaseModel):
    """Antwort von POST /v1/citations."""

    citations: list[CitationMapping]
    request_id: str


# ---------------------------------------------------------------------------
# 4.2  Nutzerfeedback (Evaluation Service – POST /v1/log)
#      Wird in PostgreSQL gespeichert für späteres Fine-Tuning.
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """Request für POST /v1/log – Nutzerfeedback zu einer Antwort."""

    query_id: str
    feedback: str  # FeedbackType-Wert: "positive" oder "negative"
    comment: str = ""


# ===================================================================
# Admin-Interfaces
# ===================================================================
#
# Frontend → Evaluation Service (Proxy)
# Verwaltung von Dokumenten, Queries, Agent-Memory und System-Prompts


class MemoryUpdate(BaseModel):
    """Request für PUT /v1/admin/memory/{use_case}."""

    memory_text: str


class PromptUpdate(BaseModel):
    """Request für PUT /v1/admin/prompts/{use_case}."""

    role: str = "default"
    prompt: str


# ===================================================================
# Service-Abhängigkeitsmatrix
# ===================================================================
#
# Aufrufer ↓ / Ziel →   | Clean | Structure | Embed | VectorDB | Eval | Ollama | Qdrant | Postgres
# -----------------------+-------+-----------+-------+----------+------+--------+--------+---------
# Frontend               |   ✓   |     ✓     |   ✓   |    ✓     |  ✓   |        |        |
# Evaluation             |       |           |   ✓   |    ✓     |      |   ✓    |        |    ✓
# Embedding              |       |           |       |          |      |   ✓    |        |
# VectorDB               |       |           |       |          |      |        |   ✓    |
# Cleaning               |       |           |       |          |      |        |        |
# Data Structure          |       |           |       |          |      |        |        |
#
#
# Konfiguration (Umgebungsvariablen):
#
#   CLEANING_SERVICE_URL       Frontend         http://cleaning:8001
#   DATA_STRUCTURE_SERVICE_URL Frontend         http://data_structure:8002
#   EMBEDDING_SERVICE_URL      Frontend, Eval   http://embedding:8003
#   VECTORDB_SERVICE_URL       Frontend, Eval   http://vectordb:8004
#   EVALUATION_SERVICE_URL     Frontend         http://evaluation:8005
#   OLLAMA_BASE_URL            Embedding, Eval  http://ollama:11434
#   EMBEDDING_MODEL            Embedding        qwen3-embedding:0.6b
#   LLM_MODEL                  Evaluation       qwen2.5:14b
#   QDRANT_HOST                VectorDB         qdrant
#   QDRANT_PORT                VectorDB         6333
#   DATABASE_URL               Evaluation       PostgreSQL Connection String
#   AGENT_MAX_STEPS            Evaluation       5

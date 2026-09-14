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
    images: list[dict] = Field(default_factory=list)


class CNCStructureRequest(BaseModel):
    markdown: str
    metadata: dict = Field(default_factory=dict)
    config: StructureConfig = Field(default_factory=StructureConfig)


class FolderPlanRequest(BaseModel):
    """Relative Pfade eines gewählten Ordners (ohne Inhalte)."""

    paths: list[str] = Field(default_factory=list, max_length=50_000)


class ProductFile(BaseModel):
    path: str
    content_b64: str


class ProductFilesRequest(BaseModel):
    """Die Dateien eines Produkts; der Pfad trägt Maschine und Kategorie.

    ``machine``/``category`` überschreiben den Pfad – für Produktordner, die
    ohne ihren Maschinenordner hochgeladen werden und deren Maschine aus dem
    Bestand übernommen oder vom Nutzer gewählt wurde.
    """

    files: list[ProductFile] = Field(default_factory=list, max_length=500)
    machine: str | None = None
    category: str | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str

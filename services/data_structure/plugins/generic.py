"""Plugin für Use Cases, die im Dashboard angelegt wurden.

Ein neuer Use Case entsteht im Dashboard als Datenbankzeile – ohne eigenen
Code. Der Strukturierungsdienst kannte bisher nur die eingebauten Plugins und
lehnte jeden Upload dafür mit ``unknown_use_case`` ab. Dieses Plugin übernimmt
die generische Verarbeitung: Chunks landen in der Default-Collection des Use
Case, vom Aufrufer mitgegebene Metadaten (``config.extra``) stehen bereits in
jedem Chunk (der Chunker legt sie an) und bleiben unverändert.
"""

from __future__ import annotations

from shared.models import ChunkMetadata

from plugins.base import BasePlugin


class GenericPlugin(BasePlugin):
    """Ohne Use-Case-spezifische Metadaten-Logik."""

    def __init__(self, use_case_id: str, default_collection: str) -> None:
        self.use_case_id = use_case_id
        self._default_collection = default_collection

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        return base

    def get_collections(self) -> list[str]:
        return [self._default_collection]

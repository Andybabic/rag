"""Plugin für Use Case 'ustp'.

Generiert von scripts/add_usecase.py. Ergänzen Sie enrich_metadata() mit
Ihrer use-case-spezifischen Logik. Prompt, Actions und Default-Collection
werden aus prompts/use_cases_registry.json gelesen.
"""

from __future__ import annotations

from shared.models import ChunkMetadata

from plugins.base import BasePlugin


class UstpPlugin(BasePlugin):
    use_case_id = "ustp"

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        """Use-Case-spezifische Metadaten.

        Standard: alle Request-Felder nach base.extra übernehmen.
        Hier eigene Regeln ergänzen, z.B.:
            base.extra["kategorie"] = raw_meta.get("kategorie", "unbekannt")
        """
        base.extra = {**(base.extra or {}), **raw_meta}
        return base

    def get_collections(self) -> list[str]:
        return ["ustp_default"]

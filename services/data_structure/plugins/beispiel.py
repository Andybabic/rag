"""Beispiel-Plugin: Vollständig kommentierte Vorlage für neue Use Cases.

Dieses Plugin dient als Referenz. Kopieren Sie diese Datei und passen
Sie die markierten Stellen an.

Schritte:
1. Datei kopieren: cp plugins/beispiel.py plugins/mein_usecase.py
2. Klasse und use_case_id anpassen
3. enrich_metadata() implementieren
4. Optional: get_collections(), get_system_prompt(), get_agent_actions()
5. In plugins/__init__.py importieren und register_plugin() aufrufen
"""

from __future__ import annotations

from shared.models import ChunkMetadata

from plugins.base import BasePlugin


class BeispielPlugin(BasePlugin):
    # ① Eindeutiges Kürzel – wird in API-Requests als "use_case" übergeben
    use_case_id = "beispiel"

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        """② Pflichtmethode: Metadaten anreichern.

        - base enthält bereits file_name, page, doc_type, use_case, collection
        - text ist der Chunk-Text (für regelbasierte Erkennung)
        - raw_meta kommt aus dem Request (config.extra)

        Schreiben Sie Ihre Felder in base.extra:
        """
        # Beispiel: ein Feld aus dem Request übernehmen
        kategorie = raw_meta.get("kategorie", "unbekannt")

        # Beispiel: regelbasierte Erkennung aus dem Text
        ist_wichtig = "ACHTUNG" in text or "WARNUNG" in text

        base.extra = {
            **raw_meta,                  # alle Request-Felder beibehalten
            "kategorie": kategorie,      # zusätzliches Feld
            "ist_wichtig": ist_wichtig,  # aus Text abgeleitet
        }
        return base

    def get_collections(self) -> list[str]:
        """③ Optional: Welche Qdrant-Collections verwendet werden.

        Der erste Eintrag ist die Default-Collection.
        """
        return ["beispiel_docs"]

    def get_system_prompt(self, role: str = "default") -> str:
        """④ Optional: System-Prompt für den LLM-Agent.

        Der role-Parameter erlaubt verschiedene Prompts je Zielgruppe.
        """
        return (
            "Du bist ein Assistent für das Beispiel-System. "
            "Beantworte Fragen basierend auf dem Kontext."
        )

    def get_agent_actions(self) -> list[str]:
        """⑤ Optional: Verfügbare Agent-Actions.

        Standard-Actions: SEARCH, RECALL_MEMORY, LOOKUP_SOURCES, FINAL_ANSWER
        Eigene Actions können hier ergänzt werden.
        """
        return ["SEARCH", "RECALL_MEMORY", "LOOKUP_SOURCES", "FINAL_ANSWER"]

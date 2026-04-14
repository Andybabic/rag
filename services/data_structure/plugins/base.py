"""Basis-Klasse für alle Use-Case-Plugins.

Neue Use Cases implementieren:
1. Diese Klasse erben
2. use_case_id setzen
3. enrich_metadata() implementieren
4. Optional: get_collections(), get_system_prompt(), get_agent_actions() überschreiben
5. In plugins/__init__.py registrieren

Beispiel-Plugin: plugins/beispiel.py
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from shared.models import ChunkMetadata


class BasePlugin(ABC):
    """Basis-Klasse für alle Use-Case-Plugins.

    Jeder Use Case der Plattform wird durch genau ein Plugin repräsentiert.
    Das Plugin steuert:
    - Welche Metadaten an Chunks angehängt werden (enrich_metadata)
    - In welche Qdrant-Collections geschrieben wird (get_collections)
    - Welchen System-Prompt der LLM-Agent verwendet (get_system_prompt)
    - Welche Agent-Actions verfügbar sind (get_agent_actions)
    """

    use_case_id: str = ""

    @abstractmethod
    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        """Use-Case-spezifische Metadaten hinzufügen.

        Wird für jeden Chunk einmal aufgerufen. Schreibt in base.extra
        und kann base.collection überschreiben.

        Args:
            base: Die vorausgefüllte ChunkMetadata (file_name, page, etc.
                  sind bereits gesetzt).
            text: Der Chunk-Text – kann für regelbasierte Erkennung
                  verwendet werden (z.B. Thema aus Schlüsselwörtern).
            raw_meta: Felder aus dem Request config.extra – vom Aufrufer
                      mitgegebene Zusatzinformationen.

        Returns:
            Die angereicherte ChunkMetadata (darf dasselbe Objekt sein).
        """
        ...

    def get_collections(self) -> list[str]:
        """Alle Qdrant-Collections die dieser Use Case verwendet.

        Der erste Eintrag gilt als Default-Collection wenn im Request
        keine target_collection angegeben wird.
        """
        return [f"{self.use_case_id}_default"]

    def get_system_prompt(self, role: str = "default") -> str:
        """System-Prompt für den LLM-Agent.

        Args:
            role: Optionale Rolle (z.B. "trainee" vs "expert") um den
                  Prompt an die Zielgruppe anzupassen.
        """
        return (
            "Beantworte Fragen ausschließlich basierend auf dem "
            "bereitgestellten Kontext. Wenn du die Antwort nicht im "
            "Kontext findest, sage das klar."
        )

    def get_agent_actions(self) -> list[str]:
        """Verfügbare Agent-Actions für diesen Use Case.

        Diese Liste bestimmt welche Tools der ReAct-Agent verwenden darf.
        """
        return ["SEARCH", "RECALL_MEMORY", "LOOKUP_SOURCES", "FINAL_ANSWER"]

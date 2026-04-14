"""Plugin: Firma Neumann – Maschinenwartung.

Techniker beschreiben Störungsfälle in natürlicher Sprache.
Das System durchsucht Wartungshandbücher und liefert Behebungsvorschläge.
"""

from __future__ import annotations

from shared.models import ChunkMetadata

from plugins.base import BasePlugin

_AREA_KEYWORDS: dict[str, list[str]] = {
    "hydraulik": ["druck", "öl", "pumpe", "ventil", "hydraulik"],
    "elektrik": ["spannung", "strom", "motor", "frequenz"],
    "mechanik": ["lager", "welle", "getriebe", "riemen"],
}

_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "stoerung": ["fehler", "störung", "alarm", "warnung"],
    "wartung": ["wartung", "inspektion", "prüfung", "intervall"],
    "sicherheit": ["sicherheit", "gefahr", "schutz", "vorsicht"],
}


def _detect(text: str, keywords: dict[str, list[str]], default: str) -> str:
    """Regelbasierte Erkennung aus Chunk-Text."""
    lower = text.lower()
    for label, kws in keywords.items():
        if any(kw in lower for kw in kws):
            return label
    return default


class NeumannPlugin(BasePlugin):
    use_case_id = "neumann"

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        machine_id = raw_meta.get("machine_id", "unknown")
        area = raw_meta.get("area") or _detect(text, _AREA_KEYWORDS, "allgemein")
        topic = _detect(text, _TOPIC_KEYWORDS, "allgemein")

        base.extra = {
            **raw_meta,
            "machine_id": machine_id,
            "area": area,
            "topic": topic,
        }
        base.collection = f"neumann_{machine_id.lower().replace('-', '_')}"
        return base

    def get_collections(self) -> list[str]:
        return ["neumann_machines"]

    def get_system_prompt(self, role: str = "default") -> str:
        return (
            "Du bist ein Wartungsassistent für industrielle Maschinen. "
            "Beantworte Störungsmeldungen basierend auf den Wartungshandbüchern. "
            "Gib immer die exakte Quelle (Dokument, Seite) an. "
            "Wenn du keine passende Information findest, sage das klar."
        )

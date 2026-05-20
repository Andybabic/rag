"""Plugin: Wiener Linien – rollenbasierter Wissensassistent.

Unterscheidet automatisch zwischen Auszubildenden (einfache Sprache)
und Fachpersonal (Vorschriften, Paragraphen). Gesetzestexte werden
mit korrekter Paragraphenangabe zitiert.
"""

from __future__ import annotations

import re

from shared.models import ChunkMetadata

from plugins.base import BasePlugin

LAW_PATTERN = re.compile(r"§\s*\d+\s*(Abs\.\s*\d+)?\s*\w+")

_TECHNICAL_TERMS = {"frequenz", "impedanz", "nennspannung"}


def _estimate_difficulty(text: str, has_law_ref: bool) -> int:
    """Estimate difficulty 1–5."""
    difficulty = 1
    word_count = len(text.split())
    if word_count > 200:
        difficulty += 1
    if word_count > 400:
        difficulty += 1
    if has_law_ref:
        difficulty += 1
    lower = text.lower()
    if any(term in lower for term in _TECHNICAL_TERMS):
        difficulty += 1
    return min(difficulty, 5)


class WienerLinienPlugin(BasePlugin):
    """Rollenbasierter Wissensassistent für Wiener Linien.

    Topic-basierte Collection-Aufteilung wurde entfernt: das naive
    Keyword-Matching legte z. B. §11 Ersatzsignal (Fahrzeug-Inhalt
    mit „Drucktaste am Armaturenpult") in ``wl_strecke``, weil das
    Wort „Signal"/„Bahnsteig" mehr Treffer hatte als „bremse". Folge:
    suchte der Agent in ``wl_fahrzeug``, blieb §11 unauffindbar.
    Alle Chunks landen jetzt in einer einzigen ``wl_default``-Collection.
    """

    use_case_id = "wiener_linien"

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        law_match = LAW_PATTERN.search(text)
        law_ref = law_match.group(0).strip() if law_match else ""
        has_law_ref = bool(law_ref)

        base.extra = {
            **raw_meta,
            "audience": raw_meta.get("audience", "both"),
            "difficulty": _estimate_difficulty(text, has_law_ref),
            "law_ref": law_ref,
            "criticality": "high" if has_law_ref else "medium",
        }
        # Single collection per use case – no topic-based routing.
        return base

    def get_collections(self) -> list[str]:
        return ["wl_default"]

    def get_system_prompt(self, role: str = "default") -> str:
        if role == "trainee":
            return (
                "Du bist ein Lern-Assistent für Auszubildende der Wiener Linien. "
                "Erkläre Konzepte in einfacher, verständlicher Sprache. "
                "Verwende Beispiele aus dem Alltag. "
                "Bei Unklarheit frage nach, bevor du antwortest. "
                "Gib die Quelle (Dokument, Seite) an."
            )
        return (
            "Du bist ein Wissensassistent für Fachpersonal der Wiener Linien. "
            "Beantworte Fragen fachlich präzise mit direkten Handlungsschritten. "
            "Zitiere Gesetzestexte mit korrekter §-Angabe. "
            "Gib immer Dokument und Seitenzahl an."
        )

    def get_agent_actions(self) -> list[str]:
        return [
            "SEARCH",
            "CLARIFY",
            "RECALL_MEMORY",
            "LOOKUP_SOURCES",
            "FINAL_ANSWER",
        ]

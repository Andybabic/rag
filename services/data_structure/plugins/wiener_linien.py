"""Plugin: Wiener Linien – rollenbasierter Wissensassistent.

Unterscheidet automatisch zwischen Auszubildenden (einfache Sprache)
und Fachpersonal (Vorschriften, Paragraphen). Gesetzestexte werden
mit korrekter Paragraphenangabe zitiert.
"""

from __future__ import annotations

import re

from shared.models import ChunkMetadata

from plugins.base import BasePlugin

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "fahrzeug": ["fahrzeug", "triebwagen", "bremse", "antrieb", "pantograph"],
    "strecke": ["strecke", "gleis", "weiche", "signal", "bahnsteig"],
    "betrieb": ["betrieb", "störung", "vorfall", "fahrplan", "disposition"],
    "recht": ["gesetz", "verordnung", "vorschrift", "§", "abs.", "bgbl"],
    "pruefung": ["prüfung", "frage", "aufgabe", "lernziel", "kompetenz"],
}

LAW_PATTERN = re.compile(r"§\s*\d+\s*(Abs\.\s*\d+)?\s*\w+")

_TECHNICAL_TERMS = {"frequenz", "impedanz", "nennspannung"}


def _detect_topic(text: str) -> str:
    """Topic with highest keyword hit count wins."""
    lower = text.lower()
    best_topic = "allgemein"
    best_count = 0
    for topic, keywords in TOPIC_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in lower)
        if count > best_count:
            best_count = count
            best_topic = topic
    return best_topic


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
    """Rollenbasierter Wissensassistent für Wiener Linien."""

    use_case_id = "wiener_linien"

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        law_match = LAW_PATTERN.search(text)
        law_ref = law_match.group(0).strip() if law_match else ""
        topic = _detect_topic(text)
        has_law_ref = bool(law_ref)

        base.extra = {
            **raw_meta,
            "audience": raw_meta.get("audience", "both"),
            "topic": topic,
            "difficulty": _estimate_difficulty(text, has_law_ref),
            "law_ref": law_ref,
            "criticality": "high" if has_law_ref or topic == "betrieb" else "medium",
        }
        base.collection = f"wl_{topic}"
        return base

    def get_collections(self) -> list[str]:
        return [
            "wl_fahrzeug",
            "wl_strecke",
            "wl_betrieb",
            "wl_recht",
            "wl_pruefung",
        ]

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

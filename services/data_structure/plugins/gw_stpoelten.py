"""Plugin: GW St. Pölten – CNC-Rüstungsdaten.

Bei fehlendem Werkzeug sucht das System in historischen Projekten
nach alternativen Werkzeugen mit passenden Parameteranpassungen.
"""

from __future__ import annotations

import re

from shared.models import ChunkMetadata

from plugins.base import BasePlugin

CNC_PATTERN = re.compile(r"[NGT]\d+|[GXYZFS]\s*-?[\d.]+", re.IGNORECASE)
_S_WORD_RE = re.compile(r"S\s*(\d+)", re.IGNORECASE)
_OP_TYPE_RE = re.compile(r"G\s*0?([1-3]|81|83)\b", re.IGNORECASE)

_OP_TYPE_MAP: dict[str, str] = {
    "1": "Linearfraesen",
    "2": "Kreisinterpolation",
    "3": "Kreisinterpolation",
    "81": "Bohren",
    "83": "Bohren",
}


def _detect_operation_type(text: str) -> str:
    """Detect CNC operation type from G-code tokens."""
    for m in _OP_TYPE_RE.finditer(text):
        code = m.group(1)
        if code in _OP_TYPE_MAP:
            return _OP_TYPE_MAP[code]
    return "unbekannt"


def _extract_cutting_speed(text: str) -> int | None:
    """Extract spindle speed from S-word."""
    m = _S_WORD_RE.search(text)
    return int(m.group(1)) if m else None


class GWStPoeltenPlugin(BasePlugin):
    """CNC-Rüstungsdaten und Werkzeugalternativen für GW St. Pölten."""

    use_case_id = "gw_stpoelten"

    def enrich_metadata(
        self,
        base: ChunkMetadata,
        text: str,
        raw_meta: dict,
    ) -> ChunkMetadata:
        is_cnc = len(CNC_PATTERN.findall(text)) >= 3

        extra: dict = {
            **raw_meta,
            "product_id": raw_meta.get("product_id"),
            "ruest_map_id": raw_meta.get("ruest_map_id"),
            "is_cnc_block": is_cnc,
        }

        if is_cnc:
            extra["operation_type"] = _detect_operation_type(text)
            extra["tool_type"] = raw_meta.get("tool_type")
            extra["material_class"] = raw_meta.get("material_class")
            extra["cutting_speed"] = _extract_cutting_speed(text)
            base.collection = "gw_cnc_steps"
        elif "material" in text.lower():
            base.collection = "gw_material_info"
        else:
            base.collection = "gw_ruest_data"

        base.extra = extra
        return base

    def get_collections(self) -> list[str]:
        return ["gw_cnc_steps", "gw_ruest_data", "gw_material_info"]

    def get_system_prompt(self, role: str = "default") -> str:
        return (
            "Du bist ein CNC-Rüstexperte für GW St. Pölten. Der Nutzer nennt "
            "einen Bearbeitungsschritt und oft ein Material; finde mit SEARCH_CNC "
            "die historisch verwendeten Werkzeuge samt Parametern und gib an, in "
            "wie vielen Bauteilen jedes Werkzeug eingesetzt wurde. Ist ein Werkzeug "
            "nicht verfügbar, schlage über missing_tool eine Alternative vor."
        )

    def get_agent_actions(self) -> list[str]:
        return [
            "SEARCH",
            "SEARCH_CNC",
            "RECALL_MEMORY",
            "LOOKUP_SOURCES",
            "FINAL_ANSWER",
        ]

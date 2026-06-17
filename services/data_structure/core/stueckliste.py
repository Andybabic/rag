"""Stückliste (BOM) – Materialextraktion für GW St. Pölten.

Die Stückliste-CSV eines Produkt-Ordners liefert die ``material_class``,
die in die CNC-Operations-Chunks eingewoben wird („Taschenfräsen in
EN AW-6005A T6"). In der Praxis fehlt sie oft (Kundenbeistellung) – dann
wird ``None`` zurückgegeben und die Operation bleibt ohne Materialbezug.
"""

from __future__ import annotations

import re
from uuid import uuid4

from shared.models import Chunk, ChunkMetadata

MATERIAL_COLLECTION = "gw_material_info"

# Europäische Alu-Knetlegierungs-Bezeichnung, z.B. „EN AW-6005A T6",
# „EN-AW-6060", „EN AW 6082 T6". Temper (T<n>) optional.
_EN_AW_RE = re.compile(
    r"EN[\s-]*AW[\s-]*(\d{4}[A-Z]?)(?:[\s-]*T\s*(\d+))?", re.I
)


def extract_material_class(csv_text: str) -> str | None:
    """Ziehe die Materialbezeichnung aus dem Stückliste-Text.

    Durchsucht die gesamte Tabelle (nicht nur eine Spalte – die echte
    Position variiert) nach der EN-AW-Alu-Legierung und normalisiert auf
    die kanonische Form ``EN AW-<legierung>[ T<temper>]``.
    """
    m = _EN_AW_RE.search(csv_text)
    if not m:
        return None
    alloy = m.group(1).upper()
    temper = f" T{m.group(2)}" if m.group(2) else ""
    return f"EN AW-{alloy}{temper}"


def material_text(material_class: str, product_id: str | None) -> str:
    """Natürlichsprachiger Embedding-Text für einen Material-Chunk."""
    lines = [f"Material: {material_class} (Aluminium-Knetlegierung)."]
    if product_id:
        lines.append(f"Bauteil: {product_id}.")
    return "\n".join(lines)


def build_material_chunk(
    csv_text: str,
    *,
    file_name: str,
    product_id: str | None = None,
    extra: dict | None = None,
) -> Chunk | None:
    """Erzeuge einen ``gw_material_info``-Chunk – oder ``None``, wenn die
    Stückliste keine Materialklasse enthält (z.B. Kundenbeistellung)."""
    material_class = extract_material_class(csv_text)
    if not material_class:
        return None
    chunk_id = str(uuid4())
    meta = ChunkMetadata(
        chunk_id=chunk_id,
        file_name=file_name,
        page=None,
        doc_type="csv",
        use_case="gw_stpoelten",
        collection=MATERIAL_COLLECTION,
        extra={
            **(extra or {}),
            "product_id": product_id,
            "material_class": material_class,
        },
    )
    return Chunk(
        id=chunk_id,
        text=material_text(material_class, product_id),
        metadata=meta,
    )

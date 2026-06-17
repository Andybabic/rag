"""Ordner-Ingestion für GW St. Pölten.

Ein Produkt-Ordner (als ZIP hochgeladen) bündelt alle Fertigungsdaten
eines Bauteils. Dieses Modul ordnet die Dateien ihren Produkten zu und
klassifiziert sie, damit jede zur richtigen Verarbeitung geleitet wird:

    CNC-Programm (oft ohne Endung) → Operations-Chunks (gw_cnc_steps)
    Stückliste (.csv)              → Materialklasse (gw_material_info)
    Einstellblatt (.pdf)           → Rüstdaten (gw_ruest_data, via Cleaning)
    Fotos (.jpg/.png)              → Rüst-Belege (optional)

Alle Dateien eines Produkts teilen die ``product_id`` (Ordnername bzw.
CNC-Header) – das ist der Join-Key, der Operation, Material und Rüstblatt
verbindet. Reine Funktionen, damit die Logik ohne laufende Services
testbar bleibt.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from core.cnc_parser import decode_cnc_bytes, is_template_file, looks_like_cnc

_PRODUCT_RE = re.compile(r"(\d{5}\.\d{3,4})")
_PRODUCT_FROM_NAME_RE = re.compile(r"(\d{5})(\d{3,4})")          # 112217238 → 11221.7238
_BEAR_RE = re.compile(r"(\d+)\s*BEAR", re.I)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
_CNC_EXTS = {".mpf", ".spf", ".nc", ".cnc"}

# CAM-Arbeitsordner: enthalten Zwischen-/Quelldateien (Duplikate der
# maschinenfertigen Programme im Produkt-Wurzelordner + Fragmente). Nicht
# kanonisch – würden Operationen doppelt zählen.
_CAM_WORKDIR_RE = re.compile(r"(^|/)hypermill(/|$)", re.I)


def is_noncanonical_path(rel_path: str) -> bool:
    """True für Vorlagen (``*VORLAGE*``/``Programm-Vorlagen-ALT``) und
    CAM-Arbeitsordner (``Hypermill/``) – beides nicht zu ingestieren."""
    return is_template_file(rel_path) or bool(_CAM_WORKDIR_RE.search(rel_path))


def classify_file(rel_path: str, data: bytes) -> str:
    """Klassifiziere eine Datei → cnc | stueckliste | einstellblatt | image | skip | other."""
    name = os.path.basename(rel_path)
    if not name or name.startswith(".") or name.startswith("~$"):
        return "skip"
    if is_noncanonical_path(rel_path):
        return "skip"

    ext = os.path.splitext(name)[1].lower()
    low = name.lower()

    if "ckliste" in low and ext == ".csv":      # StÅckliste / Stückliste
        return "stueckliste"
    if low.startswith("einstellblatt") and ext == ".pdf":
        return "einstellblatt"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _CNC_EXTS:
        return "cnc"
    if ext == "":                                # endungslos: per Inhalt prüfen
        try:
            if looks_like_cnc(decode_cnc_bytes(data)):
                return "cnc"
        except Exception:  # noqa: BLE001
            return "other"
    return "other"


def derive_product_id(rel_path: str) -> str | None:
    """Produkt-ID aus dem Pfad (Ordnername) oder Dateinamen ableiten."""
    if m := _PRODUCT_RE.search(rel_path):
        return m.group(1)
    name = os.path.basename(rel_path)
    if m := _PRODUCT_FROM_NAME_RE.match(name):
        return f"{m.group(1)}.{m.group(2)}"
    return None


def derive_bearbeitung(rel_path: str) -> str | None:
    """Bearbeitungs-Nummer aus Pfad/Dateiname ableiten (z.B. ``1.BEARBEITUNG``)."""
    if m := _BEAR_RE.search(rel_path):
        return f"{int(m.group(1))}.BEARBEITUNG"
    return None


@dataclass
class ProductGroup:
    product_id: str
    cnc: list[tuple[str, bytes]] = field(default_factory=list)
    einstellblatt: list[tuple[str, bytes]] = field(default_factory=list)
    stueckliste: tuple[str, bytes] | None = None
    images: list[str] = field(default_factory=list)
    # Hashes bereits gesehener CNC-Inhalte (interner Dedup-Schutz).
    _cnc_hashes: set[str] = field(default_factory=set, repr=False)


def group_folder(files: list[tuple[str, bytes]]) -> list[ProductGroup]:
    """Gruppiere ZIP-Einträge nach Produkt und klassifiziere sie.

    ``files`` ist eine Liste ``(relativer_pfad, bytes)``. Dateien ohne
    erkennbare ``product_id`` und ``skip``-Dateien (Vorlagen, Temp) werden
    ausgelassen.
    """
    groups: dict[str, ProductGroup] = {}

    def _group(pid: str) -> ProductGroup:
        if pid not in groups:
            groups[pid] = ProductGroup(product_id=pid)
        return groups[pid]

    for rel_path, data in files:
        kind = classify_file(rel_path, data)
        if kind in ("skip", "other"):
            continue
        pid = derive_product_id(rel_path)
        if not pid:
            continue
        g = _group(pid)
        if kind == "cnc":
            digest = hashlib.sha1(data).hexdigest()
            if digest in g._cnc_hashes:
                continue  # byte-identische Kopie an anderem Pfad
            g._cnc_hashes.add(digest)
            g.cnc.append((rel_path, data))
        elif kind == "einstellblatt":
            g.einstellblatt.append((rel_path, data))
        elif kind == "stueckliste":
            # Erste Stückliste je Produkt gewinnt (Kopien ignorieren).
            if g.stueckliste is None:
                g.stueckliste = (rel_path, data)
        elif kind == "image":
            g.images.append(rel_path)

    # Nur Produkte mit mindestens einem CNC-Programm sind ingestierbar.
    return [g for g in groups.values() if g.cnc]

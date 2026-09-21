"""Ordner-Ingestion für GW St. Pölten.

Der Datenbestand ist ein Verzeichnisbaum ``Kategorie / Maschine / Produkt``::

    Fräsen groß/Chiron 243/11221.7199/112217199_1BEAR
    Fräsen groß/Chiron 243/11221.7199/Stückliste.csv
    Fräsen groß/Chiron 243/11221.7199/Einstellblatt 1.pdf

Dieses Modul ordnet jede Datei ihrem Produkt zu und klassifiziert sie, damit
sie zur richtigen Verarbeitung geleitet wird:

    CNC-Programm (oft ohne Endung) → Operations-Chunks (gw_cnc_steps)
    Stückliste (.csv)              → Materialklasse (gw_material_info)
    Einstellblatt (.pdf)           → Rüstdaten (gw_ruest_data, via Cleaning)
    Fotos (.jpg/.png)              → Bildbeschreibung (gw_ruest_data, via Cleaning)

Einstellblätter zählen nur als PDF: die ``.doc``-Fassungen daneben sind
nicht zwingend aktuell.

Ein Produkt ist durch ``(product_id, Maschine)`` bestimmt, nicht durch die
Produkt-ID allein – dasselbe Bauteil kann auf mehreren Maschinen gefertigt
werden (``11221.6102`` auf Matec 1 und Chiron 233-2), mit eigenen
Programmen. Reine Funktionen, damit die Logik ohne laufende Services testbar
bleibt.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from core.cnc_parser import decode_cnc_bytes, is_template_file, looks_like_cnc

_PRODUCT_RE = re.compile(r"(\d{5}\.\d{3,4})")
_PRODUCT_DIR_RE = re.compile(r"^\d{5}\.\d{3,4}$")
_PRODUCT_FROM_NAME_RE = re.compile(r"(\d{5})(\d{3,4})")          # 112217238 → 11221.7238
_BEAR_RE = re.compile(r"(\d+)\s*BEAR", re.I)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
_CNC_EXTS = {".mpf", ".spf", ".nc", ".cnc"}
_DOCUMENT_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
_JUNK_NAMES = {"thumbs.db", "desktop.ini"}

# CAM-Arbeitsordner: enthalten Zwischen-/Quelldateien (Duplikate der
# maschinenfertigen Programme im Produkt-Wurzelordner + Fragmente). Nicht
# kanonisch – würden Operationen doppelt zählen.
_CAM_WORKDIR_RE = re.compile(r"(^|/)hypermill(/|$)", re.I)
# Überholte Stände („Alte Version/", „Alte Version-ohne Stufenbohrer/"):
# gleiche Werkzeuge wie das aktuelle Programm, würden sie doppelt zählen.
_OLD_VERSION_RE = re.compile(r"(^|/)alte[ _-]?version[^/]*/", re.I)
_FIXTURE_RE = re.compile(r"vorr(ichtung)?", re.I)
_REWORK_RE = re.compile(r"nacharbeit", re.I)

# Zeichen, die in deutschen Namen nie vorkommen, aber genau dann entstehen,
# wenn ein CP850-Name als Mac Roman gelesen wird: ä→Ñ, ü→Å, ß→·.
_MOJIBAKE_MARKERS = frozenset("ÑÅ·")


def repair_name(name: str) -> str:
    """Einen verstümmelten Datei-/Ordnernamen reparieren.

    Die Ordner wurden unter DOS/Windows (CP850) angelegt und auf dem Mac als
    Mac Roman gelesen: aus „Fräsen groß" wurde „FrÑsen gro·", aus
    „Stückliste" „StÅckliste". Die Rückrechnung ist verlustfrei, wird aber nur
    angewendet, wenn eines der Markerzeichen auftritt – sonst würde ein echtes
    „é" oder „ö" mit umgedeutet.
    """
    name = unicodedata.normalize("NFC", name)
    if not _MOJIBAKE_MARKERS & set(name):
        return name
    try:
        return name.encode("mac_roman").decode("cp850")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _display_name(segment: str) -> str:
    name = repair_name(segment).strip()
    # „chiron 239-1" und „Chiron 233-1" sind dieselbe Maschinenfamilie.
    return name[:1].upper() + name[1:]


def _segments(rel_path: str) -> list[str]:
    return [s for s in unicodedata.normalize("NFC", rel_path).replace("\\", "/").split("/") if s]


def _effective_ext(name: str) -> str:
    """Dateiendung – eine rein numerische gilt nicht als Endung.

    ``11120.4225`` und ``112214096.4096`` sind endungslose CNC-Programme, deren
    Name einen Punkt enthält; ``os.path.splitext`` hielt ``.4225`` für die
    Endung und die Dateien fielen durch.
    """
    ext = os.path.splitext(name)[1].lower()
    return "" if ext[1:].isdigit() else ext


def _skip_reason(rel_path: str) -> str | None:
    """Warum eine Datei nicht verarbeitet wird – oder ``None``."""
    segs = _segments(rel_path)
    name = segs[-1] if segs else ""
    if not name or name.startswith(".") or name.startswith("~$") or name.lower() in _JUNK_NAMES:
        return "systemdatei"
    path = "/".join(segs)
    if is_template_file(path):
        return "vorlage"
    if _CAM_WORKDIR_RE.search(path):
        return "cam_arbeitsordner"
    if _OLD_VERSION_RE.search(path):
        return "alte_version"
    return None


def is_noncanonical_path(rel_path: str) -> bool:
    """True für Vorlagen, CAM-Arbeitsordner (``Hypermill/``) und alte
    Stände (``Alte Version/``) – nichts davon wird ingestiert."""
    return _skip_reason(rel_path) not in (None, "systemdatei")


def program_role(rel_path: str) -> str:
    """Wofür ein CNC-Programm da ist: ``produktion``, ``vorrichtung`` oder
    ``nacharbeit``.

    Vorrichtungsprogramme fertigen die Spannvorrichtung, nicht das Bauteil;
    ihre Werkzeuge dürfen die „in N Bauteilen verwendet"-Zählung nicht
    speisen. Nacharbeit ist ein zweiter Durchgang am selben Bauteil.
    """
    path = "/".join(_segments(rel_path))
    if _FIXTURE_RE.search(path):
        return "vorrichtung"
    if _REWORK_RE.search(path):
        return "nacharbeit"
    return "produktion"


def classify_path(rel_path: str) -> str:
    """Klassifikation allein am Pfad – ohne Dateiinhalt.

    → ``skip`` | ``cnc_candidate`` | ``stueckliste`` | ``einstellblatt`` |
    ``image`` | ``document`` | ``other``. Grundlage der Upload-Planung, bei der
    der Browser nur die Pfade schickt; endungslose Dateien bleiben Kandidaten,
    bis ihr Inhalt geprüft ist.
    """
    if _skip_reason(rel_path):
        return "skip"
    name = repair_name(_segments(rel_path)[-1])
    ext = _effective_ext(name)
    low = name.lower()

    if "ckliste" in low and ext == ".csv":      # Stückliste (auch verstümmelt)
        return "stueckliste"
    if low.startswith("einstellblatt") and ext == ".pdf":
        return "einstellblatt"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _CNC_EXTS or ext == "":
        return "cnc_candidate"
    if ext in _DOCUMENT_EXTS:
        return "document"
    return "other"


def classify_file(rel_path: str, data: bytes) -> str:
    """Klassifiziere eine Datei → cnc | stueckliste | einstellblatt | image | skip | other."""
    kind = classify_path(rel_path)
    if kind == "cnc_candidate":
        if _effective_ext(_segments(rel_path)[-1]) in _CNC_EXTS:
            return "cnc"
        try:
            return "cnc" if looks_like_cnc(decode_cnc_bytes(data)) else "other"
        except Exception:  # noqa: BLE001
            return "other"
    if kind == "document":
        return "other"
    return kind


def derive_product_id(rel_path: str) -> str | None:
    """Produkt-ID aus dem Pfad (Ordnername) oder Dateinamen ableiten."""
    if m := _PRODUCT_RE.search(rel_path):
        return m.group(1)
    name = os.path.basename(rel_path)
    if m := _PRODUCT_FROM_NAME_RE.match(name):
        return f"{m.group(1)}.{m.group(2)}"
    return None


def locate_product(rel_path: str) -> tuple[str | None, str | None, str | None]:
    """``(product_id, maschine, kategorie)`` einer Datei.

    Maßgeblich ist der nächstgelegene Produktordner (``11221.7199``) über der
    Datei; Maschine und Kategorie sind die beiden Ordner darüber. Liegt nichts
    darüber (ZIP eines einzelnen Produkts), bleiben sie ``None``.
    """
    dirs = _segments(rel_path)[:-1]
    for i in range(len(dirs) - 1, -1, -1):
        if _PRODUCT_DIR_RE.match(dirs[i]):
            machine = _display_name(dirs[i - 1]) if i >= 1 else None
            category = _display_name(dirs[i - 2]) if i >= 2 else None
            return dirs[i], machine, category
    return derive_product_id(rel_path), None, None


def relative_to_product(rel_path: str) -> str:
    """Pfad ab dem Produktordner – stabil, egal wo der Upload begann."""
    segs = _segments(rel_path)
    for i in range(len(segs) - 2, -1, -1):
        if _PRODUCT_DIR_RE.match(segs[i]):
            return "/".join(repair_name(s) for s in segs[i:])
    return "/".join(repair_name(s) for s in segs)


def derive_bearbeitung(rel_path: str) -> str | None:
    """Bearbeitungs-Nummer aus Pfad/Dateiname ableiten (z.B. ``1.BEARBEITUNG``)."""
    if m := _BEAR_RE.search(rel_path):
        return f"{int(m.group(1))}.BEARBEITUNG"
    return None


@dataclass
class ProductGroup:
    product_id: str
    machine: str | None = None
    category: str | None = None
    cnc: list[tuple[str, bytes]] = field(default_factory=list)
    einstellblatt: list[tuple[str, bytes]] = field(default_factory=list)
    stueckliste: tuple[str, bytes] | None = None
    images: list[str] = field(default_factory=list)
    # Hashes bereits gesehener CNC-Inhalte (interner Dedup-Schutz).
    _cnc_hashes: set[str] = field(default_factory=set, repr=False)


def group_folder(
    files: list[tuple[str, bytes]], *, require_cnc: bool = True
) -> list[ProductGroup]:
    """Gruppiere Dateien nach ``(product_id, maschine)`` und klassifiziere sie.

    ``files`` ist eine Liste ``(relativer_pfad, bytes)``. Dateien ohne
    erkennbare ``product_id`` und ``skip``-Dateien (Vorlagen, CAM, alte
    Stände, Temp) werden ausgelassen. Mit ``require_cnc`` (Standard) fallen
    Produkte ohne CNC-Programm weg; der Produkt-Upload braucht sie trotzdem,
    damit auch deren Stückliste eingelesen wird.
    """
    groups: dict[tuple[str, str | None], ProductGroup] = {}

    for rel_path, data in files:
        kind = classify_file(rel_path, data)
        if kind in ("skip", "other"):
            continue
        pid, machine, category = locate_product(rel_path)
        if not pid:
            continue
        g = groups.get((pid, machine))
        if g is None:
            g = groups[(pid, machine)] = ProductGroup(
                product_id=pid, machine=machine, category=category
            )
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

    result = list(groups.values())
    return [g for g in result if g.cnc] if require_cnc else result


# ── Upload-Planung (nur Pfade) ────────────────────────────────────────────────


@dataclass
class PlannedProduct:
    product_id: str
    machine: str | None
    category: str | None
    cnc_candidates: list[str] = field(default_factory=list)
    stueckliste: str | None = None
    einstellblaetter: list[str] = field(default_factory=list)
    bilder: list[str] = field(default_factory=list)
    ignored_documents: list[str] = field(default_factory=list)
    variants: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def upload_paths(self) -> list[str]:
        """Dateien für den Produkt-Upload (CNC + Stückliste, ohne PDFs)."""
        return self.cnc_candidates + ([self.stueckliste] if self.stueckliste else [])


def plan_folder(paths: list[str]) -> dict:
    """Plane den Upload eines Datenbestands anhand der Pfade allein.

    Der Browser schickt nur die Pfadliste (Kilobytes statt 1,4 GB) und lädt
    danach genau die Dateien hoch, die dieser Plan nennt. So bleibt die
    Zuordnungslogik an einer Stelle – hier – statt im Frontend nachgebaut.
    """
    products: dict[tuple[str, str | None], PlannedProduct] = {}
    skipped: Counter[str] = Counter()
    unassigned = 0

    for path in paths:
        if reason := _skip_reason(path):
            skipped[reason] += 1
            continue
        kind = classify_path(path)
        pid, machine, category = locate_product(path)
        if not pid:
            unassigned += 1
            continue
        prod = products.get((pid, machine))
        if prod is None:
            prod = products[(pid, machine)] = PlannedProduct(pid, machine, category)

        if kind == "cnc_candidate":
            prod.cnc_candidates.append(path)
            if (role := program_role(path)) != "produktion":
                prod.variants.append({"path": relative_to_product(path), "role": role})
        elif kind == "stueckliste":
            prod.stueckliste = prod.stueckliste or path
        elif kind == "einstellblatt":
            prod.einstellblaetter.append(path)
        elif kind == "image":
            prod.bilder.append(path)
        elif kind == "document":
            prod.ignored_documents.append(path)

    machines_by_pid: dict[str, list[str]] = {}
    for (pid, machine) in products:
        machines_by_pid.setdefault(pid, []).append(machine or "?")

    ordered = sorted(
        products.values(),
        key=lambda p: (p.category or "", p.machine or "", p.product_id),
    )
    for prod in ordered:
        if not prod.cnc_candidates:
            prod.warnings.append("Kein CNC-Programm im Ordner")
        others = [m for m in machines_by_pid[prod.product_id] if m != (prod.machine or "?")]
        if others:
            prod.warnings.append(f"Produkt liegt auch auf: {', '.join(sorted(others))}")

    return {
        "products": [
            {
                "product_id": p.product_id,
                "machine": p.machine,
                "category": p.category,
                "cnc_candidates": p.cnc_candidates,
                "stueckliste": p.stueckliste,
                "einstellblaetter": p.einstellblaetter,
                "bilder": p.bilder,
                "upload_paths": p.upload_paths(),
                "images": len(p.bilder),
                "ignored_documents": len(p.ignored_documents),
                "ignored_paths": p.ignored_documents,
                "variants": p.variants,
                "warnings": p.warnings,
            }
            for p in ordered
        ],
        "skipped": dict(skipped),
        "unassigned": unassigned,
    }

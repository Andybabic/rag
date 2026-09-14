"""CNC-Parser für GW St. Pölten – Sinumerik/Siemens MPF-Programme.

Eine CNC-Datei (typischerweise *ohne* Dateiendung) wird in einzelne
**Werkzeug-Operationen** zerlegt. Jede Operation ist die Embedding-Einheit
für ``gw_cnc_steps`` und beantwortet die Kernfrage des Use Cases:
„Mit welchem Werkzeug (+ Parametern) wurde Operation X auf Material Y
historisch gefertigt?"

Drei Dialekte kommen in der Praxis vor (alle werden unterstützt):

* **simple** – handgeschriebene Sinumerik-Programme. Jede Maschine schreibt
  den Werkzeugwechsel etwas anders; die Beschreibung steht mal in derselben
  Zeile, mal davor, mal danach::

      T1 M16 ;DM=9 VHMI-BOHRER                  (Chiron 243/233)
      T12;DM=63/45GRAD MESSERKOPF               (Matec 1 – Wechsel via L6)
      N100 (DM=2.5 VHM-STUFENBOHRER)            (Matec 2 – Beschreibung davor)
      T78 M6
      T5 M6                                     (Chiron 233 – Beschreibung danach)
      MSG("DM=80/90 GRAD MESSERKOPF")

* **iso** – ISO/Fanuc-Programme (Chiron 239) ohne Leerzeichen zwischen den
  Wörtern und mit Klammerkommentaren::

      %
      :4442(KLEMMLASCHE PGE94110)
      N20T1M6(D.8.5/15.5/18/45 VHM-STUFENBOHRER)
      N35G73Z-11.05R5Q1F1000

* **structured** – CAM/Hypermill-Export mit Werkzeugliste + Operations-Index::

      ; --- WERKZEUGLISTE ANFANG ---
      ; T558 - GUE VHMI BOHRER D4.9 - AL47
      ; --- WERKZEUGLISTE ENDE ---
      ; N100 - 1: T558 Bohren X4,9
      ; N300 - 3: T507 Taschen Fräsen HB SF X10

Drei Daten-Eigenheiten der echten Dateien werden hier behandelt
(an realen Maschinendaten verifiziert):

1. **Gemischte Encodings** – ``ä`` liegt teils als ``0xE4`` (cp1252), teils
   als literales ``d`` (CAM-Postprozessor-Artefakt: ``Frdsen`` → ``Fräsen``)
   vor. ``decode_cnc_bytes`` + ``normalize_umlauts`` vereinheitlichen, sonst
   streut dieselbe Operation über mehrere Embeddings.
2. **Vorlagen** – ``*VORLAGE*``-Dateien / ``Programm-Vorlagen-ALT/`` sind
   Templates, keine Produktion. ``is_template_file`` filtert sie, damit sie
   die „in N Projekten verwendet"-Zählung nicht verfälschen.
3. **Kanonischer ``operation_type``** – freier Klartext (``Taschen Fräsen``,
   ``Nut Fräsen`` …) wird auf eine kleine, filterbare Vokabular-Familie
   abgebildet, robust gegen beide Schreibweisen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import uuid4

from shared.models import Chunk, ChunkMetadata


# ── Encoding & Normalisierung ─────────────────────────────────────────────────

def decode_cnc_bytes(raw: bytes) -> str:
    """Dekodiere rohe CNC-Bytes robust zu Text.

    Echte UTF-8-Exporte (mit ``Ø``) zuerst; schlägt das fehl (einzelnes
    ``0xE4`` = ``ä`` ist kein gültiges UTF-8), greift cp1252, das den
    gesamten Windows-/DOS-Zeichenraum verlustfrei abbildet.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# Bekannte Postprozessor-Verstümmelungen (ä→d) aus den echten Dateien.
# Bewusst auf eindeutige Wortfragmente beschränkt, damit kein echtes 'd'
# in anderen Wörtern (Bohren, Schruppen, …) getroffen wird.
_UMLAUT_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"C\$"), "ä"),                # FrC$sen→Fräsen (charset-Roundtrip-Artefakt)
    (re.compile(r"rdsen", re.I), "räsen"),    # Frdsen→Fräsen, Fasenfrdsen→Fasenfräsen
    (re.compile(r"frdser", re.I), "fräser"),  # Schaftfrdser→Schaftfräser (Hypermill-Werkzeugliste)
    (re.compile(r"ldngen", re.I), "längen"),  # Abldngen→Ablängen
)


def normalize_umlauts(text: str) -> str:
    """Vereinheitliche verstümmelte Umlaute auf die kanonische Schreibweise."""
    for pat, repl in _UMLAUT_FIXES:
        text = pat.sub(repl, text)
    return text


# ISO/Fanuc-Kopf: "%" allein in der ersten Zeile, dann die Programmnummer
# (":4442(…)" oder "O4442").
_ISO_HEADER_RE = re.compile(r"\A\s*%\s*\n\s*[:O]\d+")


def looks_like_cnc(text: str) -> bool:
    """Content-Sniffing: ist dieser Text ein CNC-Programm?

    Greift für endungslose Maschinendateien, bevor der Extension-Dispatch
    sie als „unbekanntes Format" ablehnt.
    """
    head = text[:2000]
    if re.search(r"%_N_.*_MPF", head):
        return True
    if _ISO_HEADER_RE.match(head.replace("\r", "")):
        return True
    # Lookbehind statt \b: ISO-Programme schreiben die Wörter ohne Leerzeichen
    # ("N10G0G53G90"). Zwischen zwei Befehlen gibt es dort keine Wortgrenze,
    # mit \b zählte ein Chiron-239-Programm deshalb kaum Tokens.
    tokens = len(re.findall(r"(?<![A-Z])[GMT]\d+(?!\d)|CYCLE\d+|MCALL", head))
    return tokens >= 8


def is_template_file(filename: str) -> bool:
    """True für Vorlagen-/Template-Dateien, die NICHT ingestiert werden sollen."""
    low = filename.lower()
    return "vorlage" in low or "programm-vorlagen" in low


# ── Kanonischer Operationstyp ─────────────────────────────────────────────────
# Priorität: erste passende Regel gewinnt (spezifisch → generisch).
_OP_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"kreistasche", re.I), "Kreistaschenfräsen"),
    (re.compile(r"taschen?\s*fr[äa]e?sen", re.I), "Taschenfräsen"),
    (re.compile(r"nut\s*fr[äa]e?sen", re.I), "Nutfräsen"),
    # Gewinde-Regeln verlangen das Verb direkt hinter „Gewinde": „G1/2 ZOLL
    # GEWINDE … VORBOHREN" ist ein Bohrschritt, kein Gewindeschritt.
    (re.compile(r"gewinde\s*-?\s*form", re.I), "Gewindeformen"),
    (re.compile(r"gewinde\s*-?\s*fr[äa]e?s", re.I), "Gewindefräsen"),
    (re.compile(r"gewinde\s*-?\s*bohr", re.I), "Gewindebohren"),
    (re.compile(r"fasen", re.I), "Fasenfräsen"),
    (re.compile(r"zentrier", re.I), "Zentrieren"),
    (re.compile(r"abl[äa]ngen", re.I), "Ablängen"),
    (re.compile(r"ansenk", re.I), "Ansenken"),
    (re.compile(r"anbohr", re.I), "Anbohren"),
    (re.compile(r"tiefloch", re.I), "Tieflochbohren"),
    (re.compile(r"\bplan|messerkopf", re.I), "Planfräsen"),
    (re.compile(r"b[üu]e?rste|entgrat", re.I), "Entgraten"),
    (re.compile(r"stichel|gravier", re.I), "Gravieren"),
    (re.compile(r"bohren", re.I), "Bohren"),
    # „FRAESER" (ASCII-Schreibweise der Maschinen) fiel früher durch.
    (re.compile(r"fr[äa]e?sen|fr[äa]e?ser", re.I), "Fräsen"),
)

# Zyklus → Operationsfamilie (Fallback, wenn kein Klartext vorhanden).
_CYCLE_OP: dict[str, str] = {
    "CYCLE81": "Bohren",
    "CYCLE82": "Bohren",
    "CYCLE83": "Tieflochbohren",
    "CYCLE84": "Gewindebohren",
    "CYCLE840": "Gewindebohren",
    # ISO-Festzyklen (Chiron 239, Matec 2 schreibt zusätzlich G83[…]).
    "G81": "Bohren",
    "G82": "Bohren",
    "G73": "Tieflochbohren",
    "G83": "Tieflochbohren",
    "G84": "Gewindebohren",
}

# Bearbeitungsstrategie (orthogonal zum Operationstyp).
_STRATEGY_RE = re.compile(r"\b(schruppen|schlichten)\b", re.I)


def classify_operation(
    text: str, cycle: str | None = None, tool_class: str | None = None
) -> str:
    """Bilde freien Operations-Klartext auf eine kanonische Familie ab.

    Reihenfolge: expliziter Klartext → Zyklus → Werkzeugklasse als
    letzter Anker (ein Fräser ohne Klartext-Verb ist eine Fräsoperation,
    z.B. „Mitte Schruppen … HB SF").
    """
    for pat, label in _OP_RULES:
        if pat.search(text):
            return label
    if cycle and cycle.upper() in _CYCLE_OP:
        return _CYCLE_OP[cycle.upper()]
    if tool_class == "Fräser":
        return "Fräsen"
    if tool_class == "Bohrer":
        return "Bohren"
    return "unbekannt"


def detect_strategy(text: str) -> str | None:
    if m := _STRATEGY_RE.search(text):
        return m.group(1).capitalize()
    return None


# ── Werkzeugtyp/-klasse + Durchmesser ─────────────────────────────────────────
_TOOL_TYPE_PATTERNS: tuple[str, ...] = (
    r"VHMI?-?\s*STUFENBOHRER", r"VHMI?-?\s*BOHRER", r"VHM-?\s*NC-?\s*ANBOHRER",
    r"VHM-?\s*ZENTRIERER", r"VHM-?\s*SONDERFR[ÄA]E?SER",
    r"(?:VHM|WPL)-?\s*GEWINDEFR[ÄA]E?SER", r"VHM-?\s*FR[ÄA]E?SER",
    r"WPL-?\s*BOHRER", r"HSS-?\s*GEWINDE(?:FORMER|BOHRER)", r"MESSERKOPF",
    r"VHM\s*FF", r"HB\s*SF", r"MAYKESTAG",
)
_TOOL_TYPE_RE = re.compile("|".join(_TOOL_TYPE_PATTERNS), re.I)
# Letzter Anker, wenn keine bekannte Bezeichnung passt: das Werkzeug-Hauptwort.
_TOOL_NOUN_RE = re.compile(
    r"[\w-]*(?:BOHRER|FR[ÄA]E?SER|SENKER|REIBAHLE|FORMER|ZENTRIERER|MESSERKOPF"
    r"|BUERSTE|BÜRSTE|STICHEL)\b|\bH?M-NC\b",
    re.I,
)

# DM=9 / D4.9 / DM.3.7 (ISO) / D.8.5/15.5 (Stufenbohrer, erstes Maß zählt).
# Der Lookbehind verhindert Treffer mitten im Wort („GRAD 5", „RUND 5").
_DIA_RE = re.compile(r"(?<![A-Z])DM?\s*[=.]?\s*(\d+(?:[.,]\d+)?)", re.I)
_OEDIA_RE = re.compile(r"[ØX]\s*(\d+[.,]?\d*)")             # Ø4,9 / X10


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def extract_tool_type(text: str) -> str | None:
    m = _TOOL_TYPE_RE.search(text) or _TOOL_NOUN_RE.search(text)
    if not m:
        return None
    tool = re.sub(r"\s+", " ", m.group(0).strip()).upper()
    # Eine Schreibweise je Werkzeug, sonst zerfällt die „in N Bauteilen"-
    # Zählung: „VHMI BOHRER" (Hypermill) und „VHMI-BOHRER" (handgeschrieben)
    # sind dasselbe. Kurze Kürzel wie „VHM FF" / „HB SF" bleiben unberührt.
    tool = re.sub(r"\b(VHMI?|WPL|HSS)[\s-]+(?=NC|[A-ZÄÖÜ]{4,})", r"\1-", tool)
    tool = re.sub(r"\bNC[\s-]+", "NC-", tool)
    return tool.replace("Ä", "AE")


def classify_tool(tool_type: str | None) -> str:
    """Grobe Werkzeugklasse für Filterung/Alternativsuche."""
    if not tool_type:
        return "unbekannt"
    t = tool_type.upper()
    if "GEWINDE" in t:
        return "Gewindewerkzeug"
    if "ANBOHRER" in t or "ZENTRIER" in t or re.fullmatch(r"H?M-NC", t):
        return "Anbohrer"
    if "BOHRER" in t:
        return "Bohrer"
    if "SONDER" in t or "MAYKESTAG" in t:
        return "Sonderwerkzeug"
    if "MESSERKOPF" in t:
        return "Fräser"
    if "SENKER" in t:
        return "Senker"
    if "REIBAHLE" in t:
        return "Reibahle"
    if "FR" in t and ("SER" in t or "FF" in t) or "SF" in t:
        return "Fräser"
    return "unbekannt"


# ── Datenmodell ───────────────────────────────────────────────────────────────
@dataclass
class Operation:
    operation_type: str = "unbekannt"   # kanonische Familie (Filter-Facette)
    operation_label: str = ""           # bereinigter Klartext
    strategy: str | None = None         # Schruppen / Schlichten
    tool_id: str | None = None          # Katalog-ID (T558) oder Magazinslot (T1)
    tool_type: str | None = None        # VHMI-BOHRER / HB SF / VHM FF …
    tool_class: str = "unbekannt"       # Bohrer / Fräser / Anbohrer / …
    diameter: float | None = None       # mm
    spindle_speed: int | None = None    # S-Wort
    feed: int | None = None             # F-Wort
    cycle: str | None = None            # CYCLE82 …
    op_no: int | None = None            # Reihenfolge laut Operations-Index
    source_line: int | None = None
    raw_comment: str = ""


@dataclass
class CNCProgram:
    product_id: str | None = None
    bearbeitung: str | None = None
    part_name: str | None = None
    drawing_no: str | None = None
    program_name: str | None = None
    dialect: str = "?"                  # "simple" | "iso" | "structured"
    tool_catalog: dict = field(default_factory=dict)
    operations: list[Operation] = field(default_factory=list)


# ── Header (alle Dialekte) ────────────────────────────────────────────────────
_PROG_RE = re.compile(r"%_N_(\S+?)_MPF")
# ISO-Programmnummer, optional mit Klammertext: ":4442(KLEMMLASCHE PGE94110)"
_ISO_PROG_RE = re.compile(r"^[:O](\d+)\s*(?:\((.*)\))?")
_PROD_RE = re.compile(r"(?<![\d.])(\d{5}\.\d{3,4})(?!\d)")
# „Z-NR.: 6AAE…", „Z-NR.O B32159-A" (Ø verstümmelt), „ZNR:Z11221…",
# „ZEICHNR. 211043" – nur am Anfang eines Kommentars ausgewertet.
_ZNR_START_RE = re.compile(r"^Z(?:EICH)?-?\s*NR", re.I)
_ZNR_RE = re.compile(r"Z(?:EICH)?-?\s*NR\.?\s*[:.O]?\s*([A-Z0-9][\w./-]*)", re.I)
_BEAR_RE = re.compile(r"(\d+)\.\s*BEARBEITUNG", re.I)
_DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$")
_ISO_COMMENT_RE = re.compile(r"^(?:N\d+\s*)?\((.*)\)\s*$")


def _header_comments(lines: list[str]) -> list[str]:
    """Kommentartexte der Kopfzeilen, dialektunabhängig.

    Sinumerik kommentiert mit ``;``, ISO mit Klammern (``N01(11221.4442)``);
    der ISO-Programmkopf ``:4225(PROFIL 5967/11221.4225/ZEICHNR. 211043/)``
    bündelt mehrere Angaben, durch ``/`` getrennt.
    """
    out: list[str] = []
    for ln in lines[:16]:
        s = ln.strip()
        if m := _ISO_PROG_RE.match(s):
            out.extend(p.strip() for p in (m.group(2) or "").split("/") if p.strip())
        elif s.startswith(";"):
            out.append(s.lstrip("; ").strip())
        elif m := _ISO_COMMENT_RE.match(s):
            out.append(m.group(1).strip())
    return out


def _is_part_name(text: str) -> bool:
    if len(re.findall(r"[A-ZÄÖÜ]", text, re.I)) < 3 or len(text) > 80:
        return False
    upper = text.upper()
    return not (
        upper.startswith("$PATH") or _DATE_RE.match(text) or "NULLPUNKT" in upper
        or set(text) <= set("=*#-_ ")
    )


def _parse_header(lines: list[str], prog: CNCProgram) -> None:
    if lines and (m := _PROG_RE.search(lines[0])):
        prog.program_name = m.group(1)
    elif m := next((m for ln in lines[:3] if (m := _ISO_PROG_RE.match(ln.strip()))), None):
        prog.program_name = m.group(1)

    # Die Bauteilbezeichnung steht vor der Zeichnungsnummer; danach folgen
    # Programmierer und Datum („GRUBER", „15.06.2023"). Deshalb gilt ein
    # Kandidat nach der Z-NR nur, wenn davor keiner zu finden war.
    before_znr: str | None = None
    after_znr: str | None = None
    seen_znr = False
    for c in _header_comments(lines):
        if not prog.product_id and (m := _PROD_RE.search(c)):
            prog.product_id = m.group(1)
            c = _PROD_RE.sub("", c).strip(" /,")
            if not c:
                continue
        if _ZNR_START_RE.match(c):
            seen_znr = True
            if not prog.drawing_no and (m := _ZNR_RE.match(c)):
                prog.drawing_no = m.group(1)
            continue
        if not prog.bearbeitung and (m := _BEAR_RE.search(c)):
            prog.bearbeitung = f"{int(m.group(1))}.BEARBEITUNG"
            continue
        if not _is_part_name(c):
            continue
        if not seen_znr:
            before_znr = before_znr or c
        elif after_znr is None and len(re.sub(r"\W", "", c)) >= 8:
            after_znr = c
    prog.part_name = before_znr or after_znr


# ── Dialekt: simple / iso ─────────────────────────────────────────────────────
# Ein Werkzeugwechsel ist T<n> zusammen mit M6/M06/M16 – mit oder ohne
# Leerzeichen („N20T3M6"), mit Satznummer und Kommentar (;… oder (…)).
_TOOLCHANGE_RE = re.compile(r"^(?:N\d+\s*)?T(\d+)\s*(?:M0?6|M16)(?!\d)(?:\[\d+\])?(.*)$")
# T<n> mit Beschreibung, der Wechsel selbst folgt (M16-Zeile, Matec-L6).
_TOOL_DESC_RE = re.compile(r"^(?:N\d+\s*)?T(\d+)\s*(;.+|\(.+\))\s*$")
# Nacktes T<n>: nur eine Vorwahl – außer die nächste Zeile wechselt.
_BARE_TOOL_RE = re.compile(r"^(?:N\d+\s*)?T(\d+)\s*$")
_CHANGE_ONLY_RE = re.compile(r"^(?:N\d+\s*)?(?:M0?6|M16)(?!\d)")
_PAREN_NOTE_RE = re.compile(r"^(?:N\d+\s*)?\((.*[A-ZÄÖÜ].*)\)\s*$", re.I)
_MSG_RE = re.compile(r'^MSG\s*\(\s*"(.*?)"')
_SEPARATOR_RE = re.compile(r"^[=*#\-_\s]{3,}$")
_PRESELECT_NOTE_RE = re.compile(r"N(?:AE|Ä)CHSTES\s+WERKZEUG", re.I)
# CAM-Steuerkommentare („NCG#CYC82#\\CST.DIR\\bohren.com#…") sind keine
# Beschreibung für Menschen.
_CAM_META_RE = re.compile(r"#|\\|\*[A-Z]{2}\*")

# Parameter stehen in ISO-Programmen ohne Wortgrenze („Y0S8000M3") – daher
# Lookbehind auf Buchstaben statt \b. Drehzahl ≥3-stellig, Vorschub ≥2.
_S_RE = re.compile(r"(?<![A-Z_])S(\d{3,})(?!\d)")
_F_RE = re.compile(r"(?<![A-Z_])F(\d{2,})(?:\.\d+)?(?!\d)")
_DWELL_RE = re.compile(r"(?<![A-Z\d])G0?4(?!\d)")   # G4 F0.5 ist Verweilzeit
_CYCLE_RE = re.compile(r"(CYCLE\d+)|(?<![A-Z])G(73|8[1-4])(?!\d)")
_NOTE_WINDOW = 6   # Beschreibungskommentar nur direkt am Werkzeugwechsel


def _inline_desc(rest: str) -> str:
    rest = rest.strip()
    if m := re.search(r";\s*(.*)$", rest):
        return m.group(1).strip()
    if m := re.search(r"\((.*)\)", rest):
        return m.group(1).strip()
    return ""


def _nonempty(lines: list[str], start: int, step: int, limit: int) -> list[str]:
    out: list[str] = []
    i = start
    while 0 <= i < len(lines) and len(out) < limit:
        if lines[i]:
            out.append(lines[i])
        i += step
    return out


def _neighbour_desc(lines: list[str], i: int) -> str:
    """Beschreibung eines Wechsels, der selbst keine trägt.

    Matec 2 schreibt sie davor („N100 (DM=2.5 VHM-STUFENBOHRER)"),
    Chiron 233 danach („MSG("DM=80/90 GRAD MESSERKOPF")").
    """
    for prev in _nonempty(lines, i - 1, -1, 2):
        if m := _PAREN_NOTE_RE.match(prev):
            return m.group(1).strip()
    for nxt in _nonempty(lines, i + 1, 1, 3):
        if m := _MSG_RE.match(nxt):
            return m.group(1).strip()
    return ""


def _next_is_change(lines: list[str], i: int) -> bool:
    for nxt in _nonempty(lines, i + 1, 1, 2):
        if _CHANGE_ONLY_RE.match(nxt):
            return True
        if nxt.lstrip("N0123456789 ").startswith("T"):
            return False
    return False


def _tool_change_at(lines: list[str], i: int) -> tuple[str | None, str]:
    """(Werkzeugnummer, Beschreibung), wenn Zeile ``i`` ein Werkzeug einwechselt."""
    s = lines[i]
    if m := _TOOLCHANGE_RE.match(s):
        return m.group(1), _inline_desc(m.group(2)) or _neighbour_desc(lines, i)
    if (m := _TOOL_DESC_RE.match(s)) and not _PRESELECT_NOTE_RE.search(m.group(2)):
        return m.group(1), _inline_desc(m.group(2))
    if (m := _BARE_TOOL_RE.match(s)) and _next_is_change(lines, i):
        return m.group(1), _neighbour_desc(lines, i)
    return None, ""


def _body_note(s: str) -> str:
    """Erster erklärender Kommentar im Operationskörper („PLAN VORSCHRUPPEN")."""
    if m := _MSG_RE.match(s):
        text = m.group(1)
    elif s.startswith(";"):
        text = s.lstrip("; ")
    else:
        return ""
    text = text.strip()
    if (
        _SEPARATOR_RE.match(text)
        or _CAM_META_RE.search(text)
        or _PRESELECT_NOTE_RE.search(text)
        or len(re.findall(r"[A-ZÄÖÜ]", text, re.I)) < 4
    ):
        return ""
    return text


def _new_operation(tool_no: str, desc: str, line: int) -> Operation:
    tool_type = extract_tool_type(desc)
    return Operation(
        tool_id=f"T{tool_no}",
        tool_type=tool_type,
        tool_class=classify_tool(tool_type),
        diameter=_to_float(d.group(1)) if (d := _DIA_RE.search(desc)) else None,
        operation_label=desc,
        source_line=line,
        raw_comment=desc,
    )


def _parse_simple(lines: list[str], prog: CNCProgram) -> None:
    prog.dialect = "simple"
    stripped = [ln.strip() for ln in lines]
    cur: Operation | None = None
    notes: dict[int, str] = {}
    body_seen = 0

    for i, s in enumerate(stripped):
        tool_no, desc = _tool_change_at(stripped, i)
        if tool_no is not None:
            # T0 leert die Spindel: keine Operation, und nachfolgende S/F
            # gehören zu keinem Werkzeug.
            if int(tool_no) == 0:
                cur = None
                continue
            cur = _new_operation(tool_no, desc, i + 1)
            prog.operations.append(cur)
            body_seen = 0
            continue
        if cur is None or not s:
            continue

        body_seen += 1
        # Die MSG-Zeile, die schon die Beschreibung lieferte, ist keine Notiz –
        # sonst verdrängt sie den eigentlichen Hinweis („PLAN VORSCHRUPPEN").
        if (
            id(cur) not in notes
            and body_seen <= _NOTE_WINDOW
            and (note := _body_note(s))
            and note.lower() not in cur.operation_label.lower()
        ):
            notes[id(cur)] = note
        # Parameter nur aus dem Code, nicht aus Kommentaren/Meldungen.
        code = re.sub(r"\(.*?\)", "", s.split(";", 1)[0])
        if not code or code.startswith("MSG"):
            continue
        if cur.spindle_speed is None and (m := _S_RE.search(code)):
            cur.spindle_speed = int(m.group(1))
        if cur.feed is None and not _DWELL_RE.search(code) and (m := _F_RE.search(code)):
            cur.feed = int(m.group(1))
        if cur.cycle is None and (m := _CYCLE_RE.search(code)):
            cur.cycle = m.group(1) or f"G{m.group(2)}"

    for op in prog.operations:
        note = notes.get(id(op), "")
        if note and note.lower() not in op.operation_label.lower():
            op.operation_label = f"{op.operation_label} – {note}" if op.operation_label else note
        op.strategy = detect_strategy(op.operation_label)
        op.operation_type = classify_operation(
            f"{op.tool_type or ''} {op.operation_label}", op.cycle, op.tool_class
        )
    prog.operations = _dedupe_operations(prog.operations)


def _dedupe_operations(ops: list[Operation]) -> list[Operation]:
    """Identische Operationen nur einmal zählen.

    ISO-Programme wiederholen denselben Block je Spannstelle (G54, G55 …).
    Das ist ein Arbeitsschritt, keine zwei – doppelte Chunks würden nur
    Suchplätze belegen.
    """
    seen: set[tuple] = set()
    out: list[Operation] = []
    for op in ops:
        key = (op.tool_id, op.operation_label, op.spindle_speed, op.feed, op.cycle)
        if key in seen:
            continue
        seen.add(key)
        out.append(op)
    return out


# ── Dialekt: structured ───────────────────────────────────────────────────────
_TOOLLIST_RE = re.compile(r";\s*(T\d+)\s*-\s*(.+?)\s*-\s*AL", re.I)
_OPIDX_RE = re.compile(r";\s*N\d+\s*-\s*(\d+):\s*(T\d+)\s+(.+)")
# Schneidet Operationsname vom Werkzeug-/Maßteil ab.
_LABEL_CUT_RE = re.compile(r"\b(HB\s*SF|VHM|MAYKESTAG|[ØX]\s*\d|D\d)", re.I)
# S/F stehen im strukturierten Dialekt direkt nach dem Index-Kommentar:
#   R50=2984;XY VORSCHUB  /  S19000  /  … F2984
_BODY_S_RE = re.compile(r"\bS\s*(\d{3,})\b")            # Drehzahl (≥3-stellig)
_R50_RE = re.compile(r"\bR50\s*=\s*(\d+)")             # XY-Vorschub (Hypermill)
_BODY_F_RE = re.compile(r"\bF\s*(\d{2,})\b")            # Vorschub-Fallback


def _scan_op_params(body: list[str]) -> tuple[int | None, int | None]:
    """Drehzahl + Vorschub aus dem Operations-Körper (zwischen zwei
    Index-Kommentaren) ziehen. Vorschub bevorzugt aus R50 (XY-Vorschub)."""
    spindle = feed = None
    for ln in body:
        if spindle is None and (m := _BODY_S_RE.search(ln)):
            spindle = int(m.group(1))
        if feed is None and (m := _R50_RE.search(ln)):
            feed = int(m.group(1))
    if feed is None:
        for ln in body:
            if m := _BODY_F_RE.search(ln):
                feed = int(m.group(1))
                break
    return spindle, feed


def _parse_structured(lines: list[str], prog: CNCProgram) -> None:
    prog.dialect = "structured"
    in_list = False
    for ln in lines:
        s = ln.strip()
        if "WERKZEUGLISTE ANFANG" in s:
            in_list = True
            continue
        if "WERKZEUGLISTE ENDE" in s:
            in_list = False
            continue
        if in_list and (m := _TOOLLIST_RE.search(s)):
            desc = m.group(2).strip()
            tool_type = extract_tool_type(desc)
            prog.tool_catalog[m.group(1)] = {
                "desc": desc,
                "type": tool_type,
                "diameter": _to_float(d.group(1)) if (d := _DIA_RE.search(desc)) else None,
            }

    # Index-Kommentare in Datei-Reihenfolge sammeln; jeder Operations-Körper
    # reicht vom Kommentar bis zum nächsten Index-Kommentar (dort stehen S/F).
    idx = [(i, m) for i, ln in enumerate(lines)
           if (m := _OPIDX_RE.search(ln.strip()))]
    for k, (i, m) in enumerate(idx):
        op_no = int(m.group(1))
        tool_id = m.group(2)
        rest = m.group(3).strip()
        cat = prog.tool_catalog.get(tool_id, {})
        label = _LABEL_CUT_RE.split(rest)[0].strip() or rest
        dia = _OEDIA_RE.search(rest)
        tool_type = cat.get("type") or extract_tool_type(rest)
        tool_class = classify_tool(tool_type)
        end = idx[k + 1][0] if k + 1 < len(idx) else len(lines)
        spindle, feed = _scan_op_params(lines[i + 1:end])
        prog.operations.append(Operation(
            operation_type=classify_operation(rest, None, tool_class),
            operation_label=label,
            strategy=detect_strategy(rest),
            tool_id=tool_id,
            tool_type=tool_type,
            tool_class=tool_class,
            diameter=_to_float(dia.group(1)) if dia else cat.get("diameter"),
            spindle_speed=spindle,
            feed=feed,
            op_no=op_no,
            source_line=i + 1,
            raw_comment=rest,
        ))


# ── Öffentlicher Einstieg ─────────────────────────────────────────────────────
def parse_cnc(text: str) -> CNCProgram:
    """Zerlege ein CNC-Programm in Operationen (Dialekt automatisch erkannt)."""
    text = normalize_umlauts(text)
    lines = text.splitlines()
    prog = CNCProgram()
    _parse_header(lines, prog)
    if any(_OPIDX_RE.search(ln) for ln in lines):
        _parse_structured(lines, prog)
    else:
        _parse_simple(lines, prog)
        if _ISO_HEADER_RE.match(text.replace("\r", "")[:200]):
            prog.dialect = "iso"
    return prog


def parse_cnc_bytes(raw: bytes) -> CNCProgram:
    """Bequemer Einstieg ab rohen Datei-Bytes (Encoding wird normalisiert)."""
    return parse_cnc(decode_cnc_bytes(raw))


# ── Operation → Chunk ─────────────────────────────────────────────────────────
CNC_COLLECTION = "gw_cnc_steps"


def _fmt_dia(d: float | None) -> str:
    if d is None:
        return ""
    return str(int(d)) if d == int(d) else str(d).replace(".", ",")


def operation_to_text(
    op: Operation,
    prog: CNCProgram,
    material_class: str | None = None,
    machine: str | None = None,
) -> str:
    """Natürlichsprachiger Embedding-Text einer Operation.

    Bewusst so formuliert, dass eine Nutzeranfrage
    („Taschenfräsen in Aluminium, Ø10") gut darauf matched.
    """
    lines: list[str] = []
    dia = _fmt_dia(op.diameter)
    step = op.operation_type if op.operation_type != "unbekannt" else (op.operation_label or "Bearbeitung")
    if dia:
        lines.append(f"Bearbeitungsschritt: {step}, Durchmesser {dia} mm.")
    else:
        lines.append(f"Bearbeitungsschritt: {step}.")
    if op.strategy:
        lines.append(f"Strategie: {op.strategy}.")
    if op.tool_type:
        cls = f" ({op.tool_class})" if op.tool_class != "unbekannt" else ""
        tnr = f", Werkzeug-Nr. {op.tool_id}" if op.tool_id else ""
        lines.append(f"Werkzeug: {op.tool_type}{cls}{tnr}.")
    if material_class:
        lines.append(f"Material: {material_class}.")
    ctx = ", ".join(
        p for p in (prog.product_id, prog.bearbeitung, prog.part_name) if p
    )
    if ctx:
        lines.append(f"Bauteil {ctx}.")
    # Dasselbe Bauteil läuft teils auf mehreren Maschinen mit eigenen
    # Programmen; ohne Maschine im Text wären ihre Operationen ununterscheidbar.
    if machine:
        lines.append(f"Maschine: {machine}.")
    if op.spindle_speed or op.feed:
        params = []
        if op.spindle_speed:
            params.append(f"Drehzahl {op.spindle_speed}")
        if op.feed:
            params.append(f"Vorschub {op.feed}")
        lines.append("Schnittparameter: " + ", ".join(params) + ".")
    if op.operation_label and op.operation_label.lower() not in step.lower():
        lines.append(f"Original-Bezeichnung: {op.operation_label}.")
    return "\n".join(lines)


def build_cnc_chunks(
    prog: CNCProgram,
    *,
    file_name: str,
    material_class: str | None = None,
    extra: dict | None = None,
) -> list[Chunk]:
    """Wandle ein geparstes CNC-Programm in einen Chunk je Operation.

    Jeder Chunk landet in ``gw_cnc_steps``. ``product_id`` ist der
    physische Join-Key zu Einstellblatt (``gw_ruest_data``) und Stückliste
    (``gw_material_info``) – alle Dateien eines Produkt-Ordners teilen ihn.
    """
    chunks: list[Chunk] = []
    for op in prog.operations:
        chunk_id = str(uuid4())
        op_extra = {
            **(extra or {}),
            "product_id": prog.product_id,
            "bearbeitung": prog.bearbeitung,
            "part_name": prog.part_name,
            "drawing_no": prog.drawing_no,
            "program_name": prog.program_name,
            "dialect": prog.dialect,
            "is_cnc_block": True,
            # Operations-Facetten (Filter-relevant)
            "operation_type": op.operation_type,
            "operation_label": op.operation_label,
            "strategy": op.strategy,
            "tool_id": op.tool_id,
            "tool_type": op.tool_type,
            "tool_class": op.tool_class,
            "diameter": op.diameter,
            "spindle_speed": op.spindle_speed,
            "feed": op.feed,
            "cycle": op.cycle,
            "op_no": op.op_no,
            "source_line": op.source_line,
            "material_class": material_class,
            # Join-Keys für die Cross-Collection-Suche
            "cnc_step_id": chunk_id,
        }
        meta = ChunkMetadata(
            chunk_id=chunk_id,
            file_name=file_name,
            page=None,
            doc_type="cnc",
            use_case="gw_stpoelten",
            collection=CNC_COLLECTION,
            extra=op_extra,
        )
        chunks.append(Chunk(
            id=chunk_id,
            text=operation_to_text(op, prog, material_class, (extra or {}).get("machine")),
            metadata=meta,
        ))
    return chunks

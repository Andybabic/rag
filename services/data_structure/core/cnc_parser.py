"""CNC-Parser für GW St. Pölten – Sinumerik/Siemens MPF-Programme.

Eine CNC-Datei (typischerweise *ohne* Dateiendung) wird in einzelne
**Werkzeug-Operationen** zerlegt. Jede Operation ist die Embedding-Einheit
für ``gw_cnc_steps`` und beantwortet die Kernfrage des Use Cases:
„Mit welchem Werkzeug (+ Parametern) wurde Operation X auf Material Y
historisch gefertigt?"

Zwei Dialekte kommen in der Praxis vor (beide werden unterstützt):

* **simple** – handgeschrieben, ein Werkzeugaufruf je Block::

      T1 M16 ;DM=9 VHMI-BOHRER
      G0 G54 X1155 Y28 S10900 F3000 M3 M7 M8
      MCALL CYCLE82(5,0,5,-9.5,,0.1)

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
    (re.compile(r"ldngen", re.I), "längen"),  # Abldngen→Ablängen
)


def normalize_umlauts(text: str) -> str:
    """Vereinheitliche verstümmelte Umlaute auf die kanonische Schreibweise."""
    for pat, repl in _UMLAUT_FIXES:
        text = pat.sub(repl, text)
    return text


def looks_like_cnc(text: str) -> bool:
    """Content-Sniffing: ist dieser Text ein CNC-Programm?

    Greift für endungslose Maschinendateien, bevor der Extension-Dispatch
    sie als „unbekanntes Format" ablehnt.
    """
    head = text[:2000]
    if re.search(r"%_N_.*_MPF", head):
        return True
    tokens = len(re.findall(r"\b[GMT]\d+\b|CYCLE\d+|MCALL", head))
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
    (re.compile(r"fasen", re.I), "Fasenfräsen"),
    (re.compile(r"zentrier", re.I), "Zentrieren"),
    (re.compile(r"abl[äa]ngen", re.I), "Ablängen"),
    (re.compile(r"ansenk", re.I), "Ansenken"),
    (re.compile(r"anbohr", re.I), "Anbohren"),
    (re.compile(r"tiefloch", re.I), "Tieflochbohren"),
    (re.compile(r"bohren", re.I), "Bohren"),
    (re.compile(r"fr[äa]e?sen|fr[äa]ser", re.I), "Fräsen"),
)

# Zyklus → Operationsfamilie (Fallback, wenn kein Klartext vorhanden).
_CYCLE_OP: dict[str, str] = {
    "CYCLE81": "Bohren",
    "CYCLE82": "Bohren",
    "CYCLE83": "Tieflochbohren",
    "CYCLE84": "Gewindebohren",
    "CYCLE840": "Gewindebohren",
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
    r"VHMI?-?\s*BOHRER", r"VHM-?\s*NC-?\s*ANBOHRER", r"VHM-?\s*SONDERFR[ÄA]E?SER",
    r"VHM-?\s*FR[ÄA]E?SER", r"VHM\s*FF", r"HB\s*SF", r"MAYKESTAG",
)
_TOOL_TYPE_RE = re.compile("|".join(_TOOL_TYPE_PATTERNS), re.I)

_DIA_RE = re.compile(r"D[M]?\s*=?\s*(\d+[.,]?\d*)", re.I)   # DM=9 / D4.9
_OEDIA_RE = re.compile(r"[ØX]\s*(\d+[.,]?\d*)")             # Ø4,9 / X10


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def extract_tool_type(text: str) -> str | None:
    m = _TOOL_TYPE_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(0).strip()).upper()


def classify_tool(tool_type: str | None) -> str:
    """Grobe Werkzeugklasse für Filterung/Alternativsuche."""
    if not tool_type:
        return "unbekannt"
    t = tool_type.upper()
    if "ANBOHRER" in t:
        return "Anbohrer"
    if "BOHRER" in t:
        return "Bohrer"
    if "SONDER" in t or "MAYKESTAG" in t:
        return "Sonderwerkzeug"
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
    dialect: str = "?"                  # "simple" | "structured"
    tool_catalog: dict = field(default_factory=dict)
    operations: list[Operation] = field(default_factory=list)


# ── Header (beide Dialekte) ───────────────────────────────────────────────────
_PROG_RE = re.compile(r"%_N_(\S+?)_MPF")
_PROD_RE = re.compile(r";\s*(\d{5}\.\d{3,4})\b")
_ZNR_RE = re.compile(r";\s*Z-NR\.?:\s*(\S+)", re.I)
_BEAR_RE = re.compile(r";\s*(\d+\.\s*BEARBEITUNG)", re.I)


def _parse_header(lines: list[str], prog: CNCProgram) -> None:
    if lines and (m := _PROG_RE.search(lines[0])):
        prog.program_name = m.group(1)
    for ln in lines[:14]:
        s = ln.strip()
        if not prog.product_id and (m := _PROD_RE.match(s)):
            prog.product_id = m.group(1)
        elif not prog.drawing_no and (m := _ZNR_RE.match(s)):
            prog.drawing_no = m.group(1)
        elif not prog.bearbeitung and (m := _BEAR_RE.match(s)):
            prog.bearbeitung = m.group(1).strip()
        elif not prog.part_name and s.startswith(";") and "PROFILE" in s.upper():
            prog.part_name = s.lstrip("; ").strip()


# ── Dialekt: simple ───────────────────────────────────────────────────────────
_TOOLCALL_SIMPLE = re.compile(r"^T(\d+)\s+M\d+\s*;(.*)$")
_SF_RE = re.compile(r"\bS(\d+)\b.*?\bF(\d+)\b")
_CYCLE_RE = re.compile(r"(CYCLE\d+)")


def _parse_simple(lines: list[str], prog: CNCProgram) -> None:
    prog.dialect = "simple"
    cur: Operation | None = None
    for i, ln in enumerate(lines, 1):
        if m := _TOOLCALL_SIMPLE.match(ln.strip()):
            comment = m.group(2).strip()
            tool_type = extract_tool_type(comment)
            cur = Operation(
                tool_id=f"T{m.group(1)}",
                tool_type=tool_type,
                tool_class=classify_tool(tool_type),
                diameter=_to_float(d.group(1)) if (d := _DIA_RE.search(comment)) else None,
                operation_label=comment,
                source_line=i,
                raw_comment=comment,
            )
            prog.operations.append(cur)
        elif cur is not None:
            if (sf := _SF_RE.search(ln)) and cur.spindle_speed is None:
                cur.spindle_speed = int(sf.group(1))
                cur.feed = int(sf.group(2))
            if (cy := _CYCLE_RE.search(ln)) and cur.cycle is None:
                cur.cycle = cy.group(1)
    # Operationstyp final aus Werkzeugtyp + Zyklus ableiten
    for op in prog.operations:
        op.operation_type = classify_operation(
            f"{op.tool_type or ''} {op.operation_label}", op.cycle, op.tool_class
        )


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
    op: Operation, prog: CNCProgram, material_class: str | None = None
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
            text=operation_to_text(op, prog, material_class),
            metadata=meta,
        ))
    return chunks

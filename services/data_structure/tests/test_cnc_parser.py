"""Tests für den CNC-Parser (GW St. Pölten).

Die Fixtures bilden die Eigenheiten der ECHTEN Maschinendateien nach
(an realen Daten verifiziert), ohne Kundendaten ins Repo zu legen:
beide Dialekte, drei Umlaut-Verstümmelungen, Vorlagen-Filter.
"""

from __future__ import annotations

from core.cnc_parser import (
    build_cnc_chunks,
    classify_operation,
    decode_cnc_bytes,
    is_template_file,
    looks_like_cnc,
    normalize_umlauts,
    parse_cnc,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────
SIMPLE_MPF = """%_N_112217126_MPF
;$PATH=/_N_WKS_DIR/_N_GWWG21_WPD
;11221.7126
;T-PROFILE CC1500_073A06
;Z-NR.: 6AAE00000067693-B
;GRUBER
;06.09.2023

G0 G90 G53 Y0 Z0 D0 M95
T1 M16 ;DM=9 VHMI-BOHRER
G0 G54 X1155 Y28 S10900 F3000 M3 M7 M8
MCALL CYCLE82(5,0,5,-9.5,,0.1)
X1155 Y28
MCALL
T4 M16 ;DM=16/90 VHM-NC-ANBOHRER 2 SCHNEIDEN
G0 G54 X-908 Y-39 S6000 F2000 M3 M8
MCALL CYCLE82(3,0,3,-5.5,,0.1)
M30
"""

# Strukturierter Dialekt mit den drei realen Umlaut-Verstümmelungen:
#   Frdsen     (ä -> literales 'd')
#   FrC$sen    (ä -> 'C$' Charset-Artefakt) + Cxx-Durchmesser
#   Abldngen   (ä -> 'd')
STRUCTURED_MPF = """%_N_112217238_1BEAR_MPF
;$PATH=/_N_WKS_DIR/_N_GWWG21_WPD
;11221.7238
;1.BEARBEITUNG
;CC1500_079A01 GABLE PROFILE
;Z-NR.: 6AAE00000088370-A

 ; --- WERKZEUGLISTE ANFANG ---
 ; T558 - GUE VHMI BOHRER D4.9 - AL47
 ; T507 - HB SF D10 - AL32.5
 ; T361 - VHM FF 90 4Z D12 - AL40
 ; T512 - HB SF D20 - AL60
 ; --- WERKZEUGLISTE ENDE ---

 ; N100 - 1: T558 Bohren X4,9
 ; N300 - 3: T507 Taschen Frdsen HB SF X10
 ; N600 - 6: T507 Taschen FrC$sen HB SF C10
 ; N400 - 4: T507 Nut Frdsen HB SF X10
 ; N500 - 5: T361 FasenfrC$sen VHM FF C12
 ; N700 - 7: T361 Zentrieren VHM FF X12
 ; N800 - 8: T512 Abldngen Schruppen Links 90,5 HB SF X20
 ; N900 - 9: T512 Mitte Schruppen Rechts HB SF X20
N5 T5
"""


# ── Encoding / Normalisierung ─────────────────────────────────────────────────
def test_decode_cp1252_umlaut():
    # 0xE4 = ä in cp1252, ist kein gültiges UTF-8 -> Fallback greift
    assert decode_cnc_bytes(b"Fr\xe4sen") == "Fräsen"


def test_decode_utf8_passthrough():
    assert decode_cnc_bytes("Bohren Ø10".encode("utf-8")) == "Bohren Ø10"


def test_normalize_all_three_garblings():
    assert normalize_umlauts("Frdsen") == "Fräsen"
    assert normalize_umlauts("FrC$sen") == "Fräsen"
    assert normalize_umlauts("Abldngen") == "Ablängen"
    # echtes 'd' in anderen Wörtern bleibt unangetastet
    assert normalize_umlauts("Bohren Zentrieren Schruppen") == "Bohren Zentrieren Schruppen"


# ── Content-Sniffing & Vorlagen-Filter ────────────────────────────────────────
def test_looks_like_cnc_by_header():
    assert looks_like_cnc("%_N_112217126_MPF\n;foo")


def test_looks_like_cnc_by_token_density():
    assert looks_like_cnc("G0 G90 T1 M3 S1000 F300 CYCLE82 MCALL G54 M30")


def test_plain_text_is_not_cnc():
    assert not looks_like_cnc("Sehr geehrte Damen und Herren, anbei das Angebot.")


def test_template_filter():
    assert is_template_file("11221.7168/112217168_1BEAR_VORLAGE")
    assert is_template_file("Programm-Vorlagen-ALT/112217168_2BEAR")
    assert not is_template_file("11221.7238/112217238_1BEAR")


# ── Kanonische Operationstypen ────────────────────────────────────────────────
def test_classify_canonical():
    assert classify_operation("Taschen Fräsen HB SF X10") == "Taschenfräsen"
    assert classify_operation("Nut Fräsen HB SF X10") == "Nutfräsen"
    assert classify_operation("Kreistasche Fräsen HB SF X10") == "Kreistaschenfräsen"
    assert classify_operation("Fasenfräsen VHM FF X12") == "Fasenfräsen"
    assert classify_operation("Zentrieren VHM FF X12") == "Zentrieren"
    assert classify_operation("Ablängen Schruppen Links 90,5") == "Ablängen"
    assert classify_operation("Bohren X4,9") == "Bohren"


def test_classify_via_cycle_and_toolclass():
    assert classify_operation("irgendwas", cycle="CYCLE82") == "Bohren"
    assert classify_operation("CYCLE83", cycle="CYCLE83") == "Tieflochbohren"
    # Fräser ohne Verb -> Fräsen (echter Fall: "Mitte Schruppen … HB SF")
    assert classify_operation("Mitte Schruppen Rechts", tool_class="Fräser") == "Fräsen"
    assert classify_operation("???", tool_class="Bohrer") == "Bohren"


# ── Dialekt: simple ───────────────────────────────────────────────────────────
def test_parse_simple_header_and_ops():
    prog = parse_cnc(SIMPLE_MPF)
    assert prog.dialect == "simple"
    assert prog.product_id == "11221.7126"
    assert prog.drawing_no == "6AAE00000067693-B"
    assert len(prog.operations) == 2

    op0 = prog.operations[0]
    assert op0.tool_id == "T1"
    assert op0.tool_type == "VHMI-BOHRER"
    assert op0.tool_class == "Bohrer"
    assert op0.diameter == 9.0
    assert op0.spindle_speed == 10900
    assert op0.feed == 3000
    assert op0.cycle == "CYCLE82"
    assert op0.operation_type == "Bohren"

    assert prog.operations[1].tool_class == "Anbohrer"
    assert prog.operations[1].operation_type == "Anbohren"


# ── Dialekt: structured ───────────────────────────────────────────────────────
def test_parse_structured_catalog_and_ops():
    prog = parse_cnc(STRUCTURED_MPF)
    assert prog.dialect == "structured"
    assert prog.product_id == "11221.7238"
    assert prog.bearbeitung == "1.BEARBEITUNG"
    assert len(prog.tool_catalog) == 4
    assert prog.tool_catalog["T558"]["diameter"] == 4.9

    by_no = {op.op_no: op for op in prog.operations}
    # alle Verstümmelungen sind normalisiert + korrekt klassifiziert
    assert by_no[3].operation_type == "Taschenfräsen"
    assert by_no[6].operation_type == "Taschenfräsen"   # war FrC$sen
    assert by_no[4].operation_type == "Nutfräsen"
    assert by_no[5].operation_type == "Fasenfräsen"     # war FasenfrC$sen
    assert by_no[8].operation_type == "Ablängen"        # war Abldngen
    assert by_no[8].strategy == "Schruppen"
    # Fräser ohne Verb -> Fräsen
    assert by_no[9].operation_type == "Fräsen"
    # Durchmesser aus Katalog gezogen, auch wenn Inline-Maß "C10" verstümmelt war
    assert by_no[6].diameter == 10.0
    # keine verstümmelten Labels mehr
    assert all("rdsen" not in op.operation_label.lower() for op in prog.operations)
    assert all("c$" not in op.operation_label.lower() for op in prog.operations)


# Strukturierter Körper mit S/F direkt nach dem Index-Kommentar (wie real).
STRUCTURED_WITH_PARAMS = """%_N_112217168_1BEAR_MPF
;11221.7168
;1.BEARBEITUNG

 ; --- WERKZEUGLISTE ANFANG ---
 ; T507 - HB SF D10 - AL32.5
 ; --- WERKZEUGLISTE ENDE ---

 ; N1200 - 12: T507 Taschen Fräsen HB SF X10
 R50=2984;XY VORSCHUB
 R51=2052;Z VORSCHUB
 S19000
 M3 M8
 G1 X-2418.5 Y-133 Z-4 F2984
 ; N800 - 8: T507 Nut Fräsen HB SF X10
 R50=1500;XY VORSCHUB
 G1 X-300 Y-132 Z-4 F1500
"""


def test_structured_extracts_speed_and_feed_from_body():
    prog = parse_cnc(STRUCTURED_WITH_PARAMS)
    by_no = {op.op_no: op for op in prog.operations}
    assert by_no[12].operation_type == "Taschenfräsen"
    assert by_no[12].spindle_speed == 19000
    assert by_no[12].feed == 2984          # aus R50 (XY-Vorschub)
    # nächste Operation: eigener Vorschub, keine Vermischung
    assert by_no[8].feed == 1500


def test_structured_links_tool_from_catalog():
    prog = parse_cnc(STRUCTURED_MPF)
    bohren = next(op for op in prog.operations if op.op_no == 1)
    assert bohren.tool_id == "T558"
    assert bohren.tool_class == "Bohrer"
    assert bohren.diameter == 4.9


# ── Operation → Chunk ─────────────────────────────────────────────────────────
def test_build_cnc_chunks_metadata_and_text():
    prog = parse_cnc(SIMPLE_MPF)
    chunks = build_cnc_chunks(
        prog, file_name="112217126", material_class="EN AW-6005A T6"
    )
    assert len(chunks) == 2
    c = chunks[0]
    assert c.metadata.collection == "gw_cnc_steps"
    assert c.metadata.use_case == "gw_stpoelten"
    assert c.metadata.doc_type == "cnc"
    e = c.metadata.extra
    assert e["product_id"] == "11221.7126"
    assert e["operation_type"] == "Bohren"
    assert e["tool_type"] == "VHMI-BOHRER"
    assert e["material_class"] == "EN AW-6005A T6"
    assert e["is_cnc_block"] is True
    # Join-Key zeigt auf die Chunk-ID
    assert e["cnc_step_id"] == c.id
    # Embedding-Text ist natürlichsprachig und enthält Schlüsselbegriffe
    assert "Bohren" in c.text
    assert "EN AW-6005A T6" in c.text
    assert "VHMI-BOHRER" in c.text


def test_build_cnc_chunks_without_material():
    prog = parse_cnc(STRUCTURED_MPF)
    chunks = build_cnc_chunks(prog, file_name="112217238_1BEAR")
    assert len(chunks) == len(prog.operations)
    assert all(c.metadata.extra["material_class"] is None for c in chunks)
    assert all(c.metadata.extra["product_id"] == "11221.7238" for c in chunks)

"""Tests für den CNC-Parser (GW St. Pölten).

Die Fixtures bilden die Eigenheiten der ECHTEN Maschinendateien nach
(an realen Daten verifiziert), ohne Kundendaten ins Repo zu legen:
beide Dialekte, drei Umlaut-Verstümmelungen, Vorlagen-Filter.
"""

from __future__ import annotations

from core.cnc_parser import (
    build_cnc_chunks,
    classify_operation,
    classify_tool,
    decode_cnc_bytes,
    extract_tool_type,
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


# ── Maschinen-Varianten ───────────────────────────────────────────────────────
# Nachgebaut nach den echten Programmen der jeweiligen Maschine (keine
# Kundendaten). Jede schreibt den Werkzeugwechsel anders; vor dieser
# Erweiterung lieferten alle vier 0 Operationen.

# Matec 1: Beschreibung am T-Wort, Wechsel per Unterprogramm L6, danach ein
# nacktes T als Vorwahl des nächsten Werkzeugs.
MATEC1_MPF = r"""%_N_112215805_1BEAR_MPF
;VERTEILERBLOCK
;1. BEARBEITUNG
;Z-NR.:9001-003999-A
;GRUBER

STARTPOS
T12;DM=63/45GRAD MESSERKOPF
L6
T8
G0 G54 X10 Y20 S2500 F1800 M3
;PLAN VORSCHRUPPEN
T8;DM=7 VHMI-BOHRER 12XD
L6(1)
T9
;DM=7 TIEFLOCHBOHREN + NUT VORBOHREN
;NCG#CYC82#\CST.DIR\bohren.com#NC2#2#*NCG;*RO*;*HD*
G0 X5 Y5 S9000 F900 M3
MCALL CYCLE83(3,0,3,-40)
M30
"""

# Matec 2: Beschreibung in der Zeile VOR dem Wechsel, Wechsel mit Index.
MATEC2_MPF = """;11221.5990
;PPS7429/190/W/FB
;Z-NR.: B7429-A
;05.06.2024

N100 (DM=2.5 VHM-STUFENBOHRER)
T78 M6
T79
G0 G54 X-52.8 Y-9.8 S16000 F1200 M42 M3 M8
G83[-10.900,3.000,15.000,1.000,0.000,3.000]
MCALL CYCLE83(3,0,3,-10.9,,-3,,2,0,,1,0,3,2,2,0.1,)
G80
N200 (DM=3.3 VHMI-BOHRER)
T79 M6[1]
T80
G0 G55 X81.2 Y26.1 S13000 F1000 M42 M3 M8
M30
"""

# Chiron 233: nackter Wechsel, Beschreibung als Bedienermeldung DANACH;
# die Operationen stehen im eingebetteten Unterprogramm.
CHIRON233_MPF = """%_N_112213680_MPF
;KUEHLKOERPER ACP P3 KK 1-ACHS
;11221.3680
;Z-NR.: C0039871-01
;GRUBER
L3680
M30
%_N_L3680_SPF
T5 M6
MSG("DM=80/90 GRAD MESSERKOPF SANDVIK 6 SCHNEIDEN SCHRUPPEN")
G0 G55 X-45 Y46 S9500 F8000 M3 M8
;PLAN VORSCHRUPPEN
G0 G53 D0 Z0 M95
T8 M6
MSG("DM=3 VHMI-BOHRER DETAIL A VORBOHREN")
G0 G54 X182.5 Y51 S12000 F1500 M3 M7
G4 F0.5
MCALL CYCLE81(3,,3,-7.2,7.2)
T0 M6
G0 X0 Y0 S100 F99
"""

# Chiron 239 (ISO/Fanuc): keine Leerzeichen, Klammerkommentare, derselbe
# Block je Spannstelle wiederholt.
ISO_PROGRAM = "\n".join([
    "% ",                                   # echte Dateien: Leerzeichen nach %
    ":4442(KLEMMLASCHE PGE94110)",
    "N01(11221.4442)",
    "N02(Z-NR.O B32159-A) ",
    "N03(GRUBER)",
    "N10G0G53G90G80G49X0Y0Z0M95 ",
    "N20T1M6(D.8.5/15.5/18/45 VHM-STUFENBOHRER) ",
    "N25G0G54X0Y0S8000M3",
    "N35G73Z-11.05R5Q1F1000 ",
    "N75G0G80Z50M95 ",
    "N76T0M6",
    "N100T1M6(D.8.5/15.5/18/45 VHM-STUFENBOHRER)",
    "N105G0G55X0Y0S8000M3 ",
    "N115G73Z-11.05R5Q1F1000",
    "N156T0M6 ",
    "N160T3M6(DM.3.7 VHM-BOHRER)",
    "N165G0G54.1P01X-6.3Y15.8A0S3800M3",
    "N170G81Z-5R2F400",
    "N175M99",
    "%",
    "",
])


def test_matec1_description_on_tool_word_and_preselect_ignored():
    prog = parse_cnc(MATEC1_MPF)
    assert prog.dialect == "simple"
    # Die nackten Vorwahlen T8/T9 sind keine Operationen.
    assert [op.tool_id for op in prog.operations] == ["T12", "T8"]

    face, drill = prog.operations
    assert face.tool_type == "MESSERKOPF"
    assert face.tool_class == "Fräser"
    assert face.diameter == 63.0
    assert (face.spindle_speed, face.feed) == (2500, 1800)
    assert face.operation_type == "Planfräsen"      # aus der Notiz „PLAN VORSCHRUPPEN"

    assert drill.tool_type == "VHMI-BOHRER"
    assert drill.diameter == 7.0
    assert drill.cycle == "CYCLE83"
    assert drill.operation_type == "Tieflochbohren"
    assert (drill.spindle_speed, drill.feed) == (9000, 900)
    # CAM-Steuerkommentare landen nicht in der Beschreibung
    assert "#" not in drill.operation_label


def test_matec1_header():
    prog = parse_cnc(MATEC1_MPF)
    assert prog.part_name == "VERTEILERBLOCK"
    assert prog.bearbeitung == "1.BEARBEITUNG"
    assert prog.drawing_no == "9001-003999-A"


def test_matec2_description_in_line_before_change():
    prog = parse_cnc(MATEC2_MPF)
    assert [op.tool_id for op in prog.operations] == ["T78", "T79"]
    step, drill = prog.operations
    assert step.tool_type == "VHM-STUFENBOHRER"
    assert step.diameter == 2.5
    assert step.cycle == "G83"
    assert step.operation_type == "Tieflochbohren"
    assert (step.spindle_speed, step.feed) == (16000, 1200)
    assert drill.tool_type == "VHMI-BOHRER"
    assert drill.diameter == 3.3
    assert prog.product_id == "11221.5990"
    assert prog.drawing_no == "B7429-A"


def test_chiron233_description_from_message_after_change():
    prog = parse_cnc(CHIRON233_MPF)
    assert [op.tool_id for op in prog.operations] == ["T5", "T8"]
    face, drill = prog.operations
    assert face.tool_type == "MESSERKOPF"
    assert face.diameter == 80.0
    assert face.strategy == "Schruppen"
    assert face.operation_type == "Planfräsen"
    # Die Beschreibung aus MSG und die Notiz danach stehen beide im Label.
    assert "PLAN VORSCHRUPPEN" in face.operation_label
    assert drill.tool_type == "VHMI-BOHRER"
    assert drill.cycle == "CYCLE81"
    assert drill.operation_type == "Bohren"
    # G4 F0.5 ist Verweilzeit, kein Vorschub; nach T0 zählt nichts mehr.
    assert (drill.spindle_speed, drill.feed) == (12000, 1500)
    assert prog.part_name == "KUEHLKOERPER ACP P3 KK 1-ACHS"


def test_iso_program_is_recognised_and_parsed():
    assert looks_like_cnc(ISO_PROGRAM)
    prog = parse_cnc(ISO_PROGRAM)
    assert prog.dialect == "iso"
    assert prog.program_name == "4442"
    assert prog.product_id == "11221.4442"
    assert prog.drawing_no == "B32159-A"
    assert prog.part_name == "KLEMMLASCHE PGE94110"

    # Der je Spannstelle wiederholte T1-Block zählt einmal.
    assert [op.tool_id for op in prog.operations] == ["T1", "T3"]
    step, drill = prog.operations
    assert step.tool_type == "VHM-STUFENBOHRER"
    assert step.diameter == 8.5                     # „D.8.5/15.5/…": erstes Maß
    assert step.cycle == "G73"
    assert step.operation_type == "Tieflochbohren"
    assert (step.spindle_speed, step.feed) == (8000, 1000)
    assert drill.diameter == 3.7                    # „DM.3.7"
    assert (drill.spindle_speed, drill.feed) == (3800, 400)
    assert drill.operation_type == "Bohren"


def test_preselect_with_comment_is_not_a_tool_change():
    prog = parse_cnc(
        "%_N_1_MPF\nT1 M16 ;DM=9 VHMI-BOHRER\nG0 S9000 F900\n"
        "T3 ; NAECHSTES WERKZEUG\nG1 X10\nM30\n"
    )
    assert [op.tool_id for op in prog.operations] == ["T1"]


def test_tool_spelling_is_unified():
    # Hypermill schreibt „VHMI BOHRER", handgeschriebene Programme
    # „VHMI-BOHRER" – dasselbe Werkzeug, eine Bezeichnung.
    assert extract_tool_type("GUE VHMI BOHRER D4.9") == "VHMI-BOHRER"
    assert extract_tool_type("DM=9 VHMI-BOHRER") == "VHMI-BOHRER"
    assert extract_tool_type("VHM FF 90 4Z D12") == "VHM FF"
    assert extract_tool_type(normalize_umlauts("Schaftfrdser")) == "SCHAFTFRAESER"


def test_new_tool_classes():
    assert classify_tool("HSS-GEWINDEFORMER") == "Gewindewerkzeug"
    assert classify_tool("VHM-ZENTRIERER") == "Anbohrer"
    assert classify_tool("MESSERKOPF") == "Fräser"
    assert classify_tool("VHM-STUFENBOHRER") == "Bohrer"


def test_iso_tokens_count_without_word_boundaries():
    packed = "N10G0G53G90G80G49X0Y0Z0M95\nN15M31\nN20T1M6\nN25G0G54X0Y0S8000M3\n"
    assert looks_like_cnc(packed)


def test_chunk_text_names_the_machine():
    # 11221.6102 läuft auf Matec 1 und Chiron 233-2 – der Text muss sie trennen.
    prog = parse_cnc(SIMPLE_MPF)
    [first, *_] = build_cnc_chunks(prog, file_name="x", extra={"machine": "Matec 1"})
    assert "Maschine: Matec 1." in first.text
    [no_machine, *_] = build_cnc_chunks(prog, file_name="x")
    assert "Maschine" not in no_machine.text

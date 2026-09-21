"""Tests für Ordner-Gruppierung + Stückliste-Materialextraktion (GW St. Pölten)."""

from __future__ import annotations

from core.folder_ingest import (
    classify_file,
    derive_bearbeitung,
    derive_product_id,
    group_folder,
    locate_product,
    plan_folder,
    program_role,
    relative_to_product,
    repair_name,
)
from core.stueckliste import build_material_chunk, extract_material_class

_CNC = b"%_N_112217238_1BEAR_MPF\r\n;11221.7238\r\nT1 M16 ;DM=9 VHMI-BOHRER\r\nG0 S1 F1 CYCLE82 MCALL\r\n"
_CNC3 = b"%_N_112217238_3BEAR_MPF\r\n;11221.7238\r\nT5 M16 ;DM=20 VHM-FRAESER\r\nG0 S2 F2 CYCLE82 MCALL\r\n"

# Stückliste mit Material (wie 11221.7126)
_BOM_WITH_MAT = (
    "Pos;Teil;Bezeichnung 1;Bezeichnung 2\r\n"
    "10;11021.0596XB;PROF.92x55x5400 +10/0mm;EN AW-6005A T6\r\n"
)
# Stückliste ohne Material (Kundenbeistellung, wie 11221.7238)
_BOM_NO_MAT = (
    "Pos;Teil;Bezeichnung 1;Bezeichnung 2\r\n"
    "10;11021.0620XB;Kühlkörperzuschnitt;für 11221.7168 Kundenbeistell.\r\n"
)


# ── classify_file ─────────────────────────────────────────────────────────────
def test_classify_extensionless_cnc():
    assert classify_file("11221.7238/112217238_1BEAR", _CNC) == "cnc"


def test_classify_stueckliste():
    assert classify_file("11221.7126/StÅckliste.csv", _BOM_WITH_MAT.encode("cp1252")) == "stueckliste"


def test_classify_einstellblatt_pdf():
    assert classify_file("11221.7126/Einstellblatt 3.pdf", b"%PDF-1.4") == "einstellblatt"


def test_classify_image():
    assert classify_file("11221.7126/7126_1.jpg", b"\xff\xd8\xff") == "image"


def test_classify_skips_templates_and_temp():
    assert classify_file("11221.7168/112217168_1BEAR_VORLAGE", _CNC) == "skip"
    assert classify_file("Programm-Vorlagen-ALT/112217168_2BEAR", _CNC) == "skip"
    assert classify_file("11221.7168/~$nstellblatt 3-3.doc", b"x") == "skip"


def test_classify_skips_cam_workdir():
    # Hypermill/ enthält CAM-Arbeitsdateien (Duplikate/Fragmente) -> skip
    assert classify_file("11221.7168/Hypermill/112217168_2BEAR.mpf", _CNC) == "skip"
    assert classify_file("11221.7168/Hypermill/Fase.mpf", _CNC) == "skip"


def test_classify_doc_einstellblatt_is_other():
    # .doc (altes Word) ist nicht parsebar -> nicht als Einstellblatt geführt
    assert classify_file("11221.7126/Einstellblatt 3.doc", b"x") == "other"


# ── derive_* ──────────────────────────────────────────────────────────────────
def test_derive_product_id_from_folder():
    assert derive_product_id("11221.7238/112217238_1BEAR") == "11221.7238"


def test_derive_product_id_from_filename():
    assert derive_product_id("112217126") == "11221.7126"


def test_derive_bearbeitung():
    assert derive_bearbeitung("11221.7238/112217238_3BEAR") == "3.BEARBEITUNG"
    assert derive_bearbeitung("112217126") is None


# ── group_folder ──────────────────────────────────────────────────────────────
def test_group_folder_buckets_by_product():
    files = [
        ("11221.7238/112217238_1BEAR", _CNC),
        ("11221.7238/112217238_3BEAR", _CNC3),
        ("11221.7238/StÅckliste.csv", _BOM_NO_MAT.encode("cp1252")),
        ("11221.7238/Einstellblatt 3.pdf", b"%PDF-1.4"),
        ("11221.7238/3BEAR_1.jpg", b"\xff\xd8\xff"),
        ("11221.7238/112217238_1BEAR_VORLAGE", _CNC),       # template -> skip
        ("11221.7238/Hypermill/112217238_1BEAR.mpf", _CNC), # CAM-Kopie -> skip
        ("11221.7238/112217238_1BEAR_copy", _CNC),          # byte-Dup -> dedup
    ]
    groups = group_folder(files)
    assert len(groups) == 1
    g = groups[0]
    assert g.product_id == "11221.7238"
    assert len(g.cnc) == 2            # Vorlage/CAM/Byte-Dup ausgeschlossen
    assert g.stueckliste is not None
    assert len(g.einstellblatt) == 1
    assert len(g.images) == 1


def test_group_folder_requires_cnc():
    # Produkt ohne CNC-Programm ist nicht ingestierbar
    files = [("11221.7126/StÅckliste.csv", _BOM_WITH_MAT.encode("cp1252"))]
    assert group_folder(files) == []


# ── extract_material_class ────────────────────────────────────────────────────
def test_extract_material_present():
    assert extract_material_class(_BOM_WITH_MAT) == "EN AW-6005A T6"


def test_extract_material_variants():
    assert extract_material_class("x;EN-AW-6060;y") == "EN AW-6060"
    assert extract_material_class("EN AW 6082 T6") == "EN AW-6082 T6"


def test_extract_material_absent():
    assert extract_material_class(_BOM_NO_MAT) is None


def test_build_material_chunk():
    chunk = build_material_chunk(
        _BOM_WITH_MAT, file_name="StÅckliste.csv", product_id="11221.7126"
    )
    assert chunk is not None
    assert chunk.metadata.collection == "gw_material_info"
    assert chunk.metadata.extra["material_class"] == "EN AW-6005A T6"
    assert chunk.metadata.extra["product_id"] == "11221.7126"
    assert "EN AW-6005A T6" in chunk.text


def test_build_material_chunk_none_when_absent():
    assert build_material_chunk(_BOM_NO_MAT, file_name="x.csv") is None


# ── Verstümmelte Namen ────────────────────────────────────────────────────────
def test_repair_name_reverses_cp850_read_as_mac_roman():
    assert repair_name("FrÑsen gro·") == "Fräsen groß"
    assert repair_name("StÅckliste.csv") == "Stückliste.csv"
    assert repair_name("Programme fÅr Vorrichtungen") == "Programme für Vorrichtungen"


def test_repair_name_leaves_correct_names_alone():
    assert repair_name("Chiron 243") == "Chiron 243"
    # Ein echtes „ö" darf nicht umgedeutet werden – kein Markerzeichen da.
    assert repair_name("St. Pölten") == "St. Pölten"


def test_repair_name_handles_decomposed_unicode():
    # macOS liefert Namen NFD-zerlegt: „N" + kombinierende Tilde statt „Ñ".
    decomposed = "FrN\u0303sen gro\u00b7"
    assert "\u00d1" not in decomposed
    assert repair_name(decomposed) == "Fr\u00e4sen gro\u00df"


# ── Klassifikation ────────────────────────────────────────────────────────────
def test_numeric_suffix_is_not_an_extension():
    # „11120.4225" ist ein endungsloses Programm, keine .4225-Datei.
    iso = b"% \r\n:4225(PROFIL/11221.4225/)\r\nN20T1M6(DM.5 VHM-BOHRER)\r\n"
    assert classify_file("11221.4225/11120.4225", iso) == "cnc"


def test_old_versions_are_skipped():
    assert classify_file("11221.7065/Alte Version/112217065", _CNC) == "skip"
    assert classify_file(
        "11221.7500/Alte Version-ohne Stufenbohrer/112217500_ALT", _CNC
    ) == "skip"


def test_program_role():
    assert program_role("11221.6102/112216102_VORR") == "vorrichtung"
    assert program_role("11221.6102/Vorrichtung_6102/112216102_VORR") == "vorrichtung"
    assert program_role("11221.7072/112217072_2BEAR_NACHARBEIT") == "nacharbeit"
    assert program_role("11221.7072/112217072_1BEAR") == "produktion"


# ── Produkt, Maschine, Kategorie ──────────────────────────────────────────────
def test_locate_product_reads_machine_and_category():
    assert locate_product(
        "GW/FrÑsen gro·/Chiron 243/11221.7199/112217199_1BEAR"
    ) == ("11221.7199", "Chiron 243", "Fräsen groß")
    # Einheitliche Schreibung der Maschine
    assert locate_product(
        "FrÑsen klein/chiron 239-1/11221.4095/112214095"
    ) == ("11221.4095", "Chiron 239-1", "Fräsen klein")
    # Unterordner eines Produkts gehören zum Produkt
    assert locate_product(
        "M/Chiron 233-2/11221.7500/Programme fÅr Vorrichtungen/112217500_VORR_1"
    )[:2] == ("11221.7500", "Chiron 233-2")
    # ZIP eines einzelnen Produkts: nichts darüber
    assert locate_product("11221.7238/112217238_1BEAR") == ("11221.7238", None, None)


def test_relative_to_product():
    assert relative_to_product(
        "GW/FrÑsen gro·/Chiron 243/11221.7199/Hypermill/x.mpf"
    ) == "11221.7199/Hypermill/x.mpf"


def test_same_product_on_two_machines_stays_two_groups():
    files = [
        ("G/Matec 1/11221.6102/112216102_2BEAR", _CNC),
        ("M/Chiron 233-2/11221.6102/112216102_2BEAR", _CNC3),
    ]
    groups = group_folder(files)
    assert sorted(g.machine for g in groups) == ["Chiron 233-2", "Matec 1"]
    assert {g.product_id for g in groups} == {"11221.6102"}


def test_group_folder_can_keep_products_without_cnc():
    files = [("11221.6051/StÅckliste.csv", _BOM_WITH_MAT.encode("cp1252"))]
    groups = group_folder(files, require_cnc=False)
    assert len(groups) == 1
    assert groups[0].stueckliste is not None


# ── Upload-Planung ────────────────────────────────────────────────────────────
_PLAN_PATHS = [
    "GW/FrÑsen gro·/Chiron 243/11221.7199/112217199_1BEAR",
    "GW/FrÑsen gro·/Chiron 243/11221.7199/StÅckliste.csv",
    "GW/FrÑsen gro·/Chiron 243/11221.7199/Einstellblatt 1.pdf",
    "GW/FrÑsen gro·/Chiron 243/11221.7199/Einstellblatt 1.doc",
    "GW/FrÑsen gro·/Chiron 243/11221.7199/4BEAR_1.jpg",
    "GW/FrÑsen gro·/Chiron 243/11221.7199/Hypermill/11221_7199_1BE.mpf",
    "GW/FrÑsen gro·/Chiron 243/11221.7199/Programm-Vorlagen-ALT/112217199_1BEAR_VORLAGE",
    "GW/FrÑsen gro·/Matec 2/11221.6051/StÅckliste.csv",
    "GW/FrÑsen gro·/Matec 2/11221.6051/Einstellblatt 1.pdf",
    "GW/FrÑsen gro·/Matec 1/11221.6102/112216102_2BEAR",
    "GW/FrÑsen gro·/Matec 1/11221.6102/112216102_VORR",
    "GW/FrÑsen mittel/Chiron 233-2/11221.6102/112216102_2BEAR",
    "GW/.DS_Store",
    "GW/Initial Dataset/Scan_20.08.2025_13-40-55.pdf",
]


def test_plan_folder_selects_only_what_must_be_uploaded():
    plan = plan_folder(_PLAN_PATHS)
    p = next(p for p in plan["products"] if p["product_id"] == "11221.7199")
    assert (p["machine"], p["category"]) == ("Chiron 243", "Fräsen groß")
    # CNC + Stückliste; Einstellblätter nur als PDF
    assert p["upload_paths"] == [_PLAN_PATHS[0], _PLAN_PATHS[1]]
    assert p["einstellblaetter"] == [_PLAN_PATHS[2]]
    assert p["ignored_documents"] == 1
    assert p["ignored_paths"] == [_PLAN_PATHS[3]]
    # Fotos werden mit hochgeladen – als eigene Liste, nicht im CNC-Upload
    assert p["bilder"] == [_PLAN_PATHS[4]]
    assert p["images"] == 1


def test_plan_folder_reports_skips_and_unassigned():
    plan = plan_folder(_PLAN_PATHS)
    assert plan["skipped"] == {"cam_arbeitsordner": 1, "vorlage": 1, "systemdatei": 1}
    assert plan["unassigned"] == 1      # Scan ohne Produktbezug


def test_plan_folder_warnings_and_variants():
    by_key = {(p["product_id"], p["machine"]): p for p in plan_folder(_PLAN_PATHS)["products"]}
    assert by_key[("11221.6051", "Matec 2")]["warnings"] == ["Kein CNC-Programm im Ordner"]
    matec = by_key[("11221.6102", "Matec 1")]
    assert matec["warnings"] == ["Produkt liegt auch auf: Chiron 233-2"]
    assert matec["variants"] == [
        {"path": "11221.6102/112216102_VORR", "role": "vorrichtung"}
    ]

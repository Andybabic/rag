"""Tests für Ordner-Gruppierung + Stückliste-Materialextraktion (GW St. Pölten)."""

from __future__ import annotations

from core.folder_ingest import (
    classify_file,
    derive_bearbeitung,
    derive_product_id,
    group_folder,
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

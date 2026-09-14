"""Tests for POST /v1/structure and GET /v1/use-cases."""

from __future__ import annotations

import httpx
import pytest
from main import app


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _structure_body(markdown: str = "Test content.", **overrides):
    body = {
        "markdown": markdown,
        "metadata": {"file_name": "doc.pdf", "total_pages": 1, "doc_type": "pdf"},
        "use_case": "neumann",
        "config": {
            "target_collection": "neumann_machines",
            "extra": {"machine_id": "M-4711"},
        },
    }
    body.update(overrides)
    return body


# ── POST /v1/structure ───────────────────────────────────────


@pytest.mark.anyio
async def test_structure_basic(client):
    resp = await client.post("/v1/structure", json=_structure_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "request_id" in body
    assert body["use_case"] == "neumann"
    assert body["total_chunks"] >= 1
    assert body["routing"]["collection"] == "neumann_machines"


@pytest.mark.anyio
async def test_structure_chunks_have_metadata(client):
    resp = await client.post("/v1/structure", json=_structure_body())
    body = resp.json()
    chunk = body["chunks"][0]
    assert chunk["id"]
    assert chunk["metadata"]["file_name"] == "doc.pdf"
    assert chunk["metadata"]["use_case"] == "neumann"
    assert chunk["metadata"]["collection"] == "neumann_m_4711"
    assert chunk["metadata"]["extra"]["machine_id"] == "M-4711"
    assert "area" in chunk["metadata"]["extra"]
    assert "topic" in chunk["metadata"]["extra"]


@pytest.mark.anyio
async def test_structure_with_page_anchors(client):
    pages = []
    for i in range(1, 6):
        pages.append(f"<!-- page:{i} -->\n" + f"Page {i} has detailed content here. " * 40)
    md = "\n\n".join(pages)
    resp = await client.post(
        "/v1/structure",
        json={
            **_structure_body(markdown=md),
            "metadata": {"file_name": "big.pdf", "total_pages": 5, "doc_type": "pdf"},
            "config": {
                "chunk_size": 200,
                "chunk_overlap": 20,
                "target_collection": "neumann_machines",
                "extra": {},
            },
        },
    )
    body = resp.json()
    assert body["total_chunks"] >= 5
    page_numbers = {c["metadata"]["page"] for c in body["chunks"]}
    assert 1 in page_numbers
    assert 5 in page_numbers


@pytest.mark.anyio
async def test_structure_custom_chunk_size(client):
    md = "Word " * 200
    small = _structure_body(markdown=md)
    small["config"]["chunk_size"] = 50
    small["config"]["chunk_overlap"] = 5
    resp = await client.post("/v1/structure", json=small)
    body = resp.json()
    assert body["total_chunks"] >= 5


@pytest.mark.anyio
async def test_structure_default_collection_from_plugin(client):
    body = _structure_body()
    del body["config"]["target_collection"]
    resp = await client.post("/v1/structure", json=body)
    result = resp.json()
    assert result["routing"]["collection"] == "neumann_machines"


@pytest.mark.anyio
async def test_structure_unknown_use_case(client):
    resp = await client.post(
        "/v1/structure",
        json=_structure_body(use_case="unknown_company"),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "unknown_use_case"
    assert "neumann" in body["detail"]
    assert "gw_stpoelten" in body["detail"]
    assert "wiener_linien" in body["detail"]


@pytest.mark.anyio
async def test_structure_unique_chunk_ids(client):
    md = "Content. " * 100
    resp = await client.post("/v1/structure", json=_structure_body(markdown=md))
    body = resp.json()
    ids = [c["id"] for c in body["chunks"]]
    assert len(ids) == len(set(ids))


@pytest.mark.anyio
async def test_structure_request_id_present(client):
    resp = await client.post("/v1/structure", json=_structure_body())
    body = resp.json()
    assert body["request_id"]
    assert len(body["request_id"]) > 0


# ── POST /v1/structure/cnc ─────────────────────────────────


# Minimal Sinumerik program (simple dialect) – one chunk per tool operation.
_CNC_MD = (
    "%_N_112217126_MPF\n"
    ";11221.7126\n"
    ";T-PROFILE CC1500_073A06\n"
    ";Z-NR.: 6AAE00000067693-B\n\n"
    "T1 M16 ;DM=9 VHMI-BOHRER\n"
    "G0 G54 X1155 Y28 S10900 F3000 M3 M7 M8\n"
    "MCALL CYCLE82(5,0,5,-9.5,,0.1)\n"
    "M30\n"
)


def _cnc_body(**overrides):
    body = {
        "markdown": _CNC_MD,
        "metadata": {"file_name": "112217126", "material_class": "EN AW-6005A T6"},
    }
    body.update(overrides)
    return body


@pytest.mark.anyio
async def test_cnc_endpoint_returns_operation_chunks(client):
    resp = await client.post("/v1/structure/cnc", json=_cnc_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["product_id"] == "11221.7126"
    assert body["dialect"] == "simple"
    assert body["routing"]["collection"] == "gw_cnc_steps"
    assert body["total_chunks"] == 1
    assert len(body["chunks"]) == 1


@pytest.mark.anyio
async def test_cnc_endpoint_chunk_fields(client):
    resp = await client.post("/v1/structure/cnc", json=_cnc_body())
    chunk = resp.json()["chunks"][0]
    extra = chunk["metadata"]["extra"]
    assert chunk["metadata"]["collection"] == "gw_cnc_steps"
    assert extra["operation_type"] == "Bohren"
    assert extra["tool_type"] == "VHMI-BOHRER"
    assert extra["diameter"] == 9.0
    assert extra["spindle_speed"] == 10900
    assert extra["feed"] == 3000
    assert extra["product_id"] == "11221.7126"
    assert extra["material_class"] == "EN AW-6005A T6"
    assert extra["cnc_step_id"] == chunk["id"]


@pytest.mark.anyio
async def test_cnc_endpoint_material_woven_into_text(client):
    resp = await client.post("/v1/structure/cnc", json=_cnc_body())
    chunk = resp.json()["chunks"][0]
    assert "EN AW-6005A T6" in chunk["text"]
    assert "Bohren" in chunk["text"]


# ── POST /v1/structure/folder ──────────────────────────────


def _make_zip(entries: dict) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in entries.items():
            zf.writestr(path, data)
    return buf.getvalue()


_FOLDER_ZIP = {
    "11221.7126/112217126": _CNC_MD,                       # CNC (extension-less)
    "11221.7126/StÅckliste.csv": "Pos;Bez\n10;EN AW-6005A T6\n",
    "11221.7126/Einstellblatt 3.pdf": "%PDF-1.4 fake",
    "11221.7126/7126_1.jpg": "\xff\xd8\xff",
    "11221.7126/112217126_VORLAGE": _CNC_MD,               # template → skipped
}


@pytest.mark.anyio
async def test_folder_endpoint_groups_and_links(client):
    resp = await client.post(
        "/v1/structure/folder",
        content=_make_zip(_FOLDER_ZIP),
        headers={"content-type": "application/zip"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["products"]) == 1
    prod = body["products"][0]
    assert prod["product_id"] == "11221.7126"
    assert prod["cnc_files"] == 1                  # VORLAGE excluded
    assert prod["material_class"] == "EN AW-6005A T6"
    assert prod["einstellblaetter"] == ["Einstellblatt 3.pdf"]
    assert prod["images"] == 1

    cnc = [c for c in body["chunks"] if c["metadata"]["collection"] == "gw_cnc_steps"]
    mat = [c for c in body["chunks"] if c["metadata"]["collection"] == "gw_material_info"]
    assert len(cnc) == 1
    assert len(mat) == 1
    # Material aus der Stückliste ist in die CNC-Operation eingewoben
    assert cnc[0]["metadata"]["extra"]["material_class"] == "EN AW-6005A T6"
    assert "EN AW-6005A T6" in cnc[0]["text"]


@pytest.mark.anyio
async def test_folder_endpoint_rejects_non_zip(client):
    resp = await client.post(
        "/v1/structure/folder",
        content=b"not a zip",
        headers={"content-type": "application/zip"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_zip"


# ── GET /v1/use-cases ────────────────────────────────────────


@pytest.mark.anyio
async def test_use_cases_endpoint(client):
    resp = await client.get("/v1/use-cases")
    assert resp.status_code == 200
    body = resp.json()
    assert "neumann" in body
    assert "gw_stpoelten" in body
    assert "wiener_linien" in body
    assert "default_collection" in body["neumann"]
    assert "collections" in body["neumann"]
    assert "agent_actions" in body["neumann"]


# ── GW: Upload-Plan und Produkt-Upload ────────────────────────────────────────
def _b64_file(path: str, data: bytes) -> dict:
    import base64

    return {"path": path, "content_b64": base64.b64encode(data).decode()}


@pytest.mark.anyio
async def test_structure_plan_endpoint(client):
    resp = await client.post("/v1/structure/plan", json={"paths": [
        "GW/FrÑsen gro·/Chiron 243/11221.7199/112217199_1BEAR",
        "GW/FrÑsen gro·/Chiron 243/11221.7199/Einstellblatt 1.pdf",
    ]})
    assert resp.status_code == 200
    [product] = resp.json()["products"]
    assert product["machine"] == "Chiron 243"
    assert product["category"] == "Fräsen groß"


@pytest.mark.anyio
async def test_structure_product_tags_location_and_role(client):
    cnc = b"%_N_112216102_2BEAR_MPF\r\n;11221.6102\r\nT1 M16 ;DM=9 VHMI-BOHRER\r\nG0 S9000 F900\r\n"
    fixture = cnc.replace(b"2BEAR", b"VORR").replace(b"DM=9", b"DM=12")
    base = "GW/FrÑsen mittel/Chiron 233-2/11221.6102"
    resp = await client.post("/v1/structure/product", json={"files": [
        _b64_file(f"{base}/112216102_2BEAR", cnc),
        _b64_file(f"{base}/Vorrichtung_6102/112216102_VORR", fixture),
    ]})
    assert resp.status_code == 200
    body = resp.json()
    [product] = body["products"]
    assert (product["machine"], product["category"]) == ("Chiron 233-2", "Fräsen mittel")
    assert product["operations"] == 2
    assert product["warnings"] == []

    extras = [c["metadata"]["extra"] for c in body["chunks"]]
    assert {e["source_path"]: e["program_role"] for e in extras} == {
        "11221.6102/112216102_2BEAR": "produktion",
        "11221.6102/Vorrichtung_6102/112216102_VORR": "vorrichtung",
    }
    assert all(e["machine"] == "Chiron 233-2" for e in extras)
    assert all(e["product_id"] == "11221.6102" for e in extras)


@pytest.mark.anyio
async def test_structure_product_warns_about_unparsed_program(client):
    # Ein Programm ohne erkennbaren Werkzeugwechsel: sichtbar melden.
    odd = b"%_N_112219999_MPF\r\n;11221.9999\r\nG0 G90 G54 X0 Y0 M3 M8 G1 G2 G3 G17\r\n"
    resp = await client.post("/v1/structure/product", json={"files": [
        _b64_file("GW/K/Matec 1/11221.9999/112219999", odd),
    ]})
    [product] = resp.json()["products"]
    assert product["operations"] == 0
    assert product["warnings"] == ["Keine Operation erkannt: 11221.9999/112219999"]


@pytest.mark.anyio
async def test_structure_product_rejects_bad_base64(client):
    resp = await client.post("/v1/structure/product", json={"files": [
        {"path": "GW/K/M/11221.1111/1", "content_b64": "!!! kein base64 !!!"},
    ]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_encoding"


@pytest.mark.anyio
async def test_structure_product_takes_machine_override(client):
    # Ein Produktordner ohne Maschinenordner darüber: die Maschine kommt aus
    # dem Bestand (vom Frontend ermittelt) und muss in Metadaten UND Text landen.
    cnc = b"%_N_112217199_1BEAR_MPF\r\n;11221.7199\r\nT1 M16 ;DM=9 VHMI-BOHRER\r\nG0 S9000 F900\r\n"
    resp = await client.post("/v1/structure/product", json={
        "files": [_b64_file("11221.7199/112217199_1BEAR", cnc)],
        "machine": "Chiron 243",
        "category": "Fräsen groß",
    })
    [product] = resp.json()["products"]
    assert (product["machine"], product["category"]) == ("Chiron 243", "Fräsen groß")
    [chunk] = resp.json()["chunks"]
    assert chunk["metadata"]["extra"]["machine"] == "Chiron 243"
    assert "Maschine: Chiron 243." in chunk["text"]


# ── Use Cases aus dem Dashboard ───────────────────────────────


@pytest.mark.anyio
async def test_structure_accepts_dashboard_use_case(client):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    uc = SimpleNamespace(id="kunde_x", default_collection="kunde_x_handbuecher")
    with patch("plugins.get_use_case", new=AsyncMock(return_value=uc)):
        resp = await client.post("/v1/structure", json={
            "markdown": "# Wartung\\n\\nDie Pumpe wird monatlich geprüft und bei Bedarf gereinigt.",
            "metadata": {"file_name": "handbuch.pdf", "doc_type": "pdf"},
            "use_case": "kunde_x",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["use_case"] == "kunde_x"
    # Chunks gehen in die Default-Collection des Use Case – nur dort sucht der Agent.
    assert body["routing"]["collection"] == "kunde_x_handbuecher"


@pytest.mark.anyio
async def test_structure_rejects_use_case_unknown_everywhere(client):
    from unittest.mock import AsyncMock, patch

    with patch("plugins.get_use_case", new=AsyncMock(return_value=None)):
        resp = await client.post("/v1/structure", json={
            "markdown": "x", "metadata": {}, "use_case": "tippfehler",
        })
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown_use_case"
    assert "dashboard" in resp.json()["detail"]

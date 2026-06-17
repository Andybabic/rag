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

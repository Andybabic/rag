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


def _cnc_body(**overrides):
    body = {
        "markdown": (
            "N10 G01 X100 F0.3 S1200\nN20 G01 Y50\n\n"
            "Materialangabe Stahl ST52\n\n"
            "Allgemeine Rüstanweisung für Werkzeug A."
        ),
        "metadata": {"product_id": "P-100", "ruest_map_id": "R-200"},
    }
    body.update(overrides)
    return body


@pytest.mark.anyio
async def test_cnc_endpoint_separates_lists(client):
    resp = await client.post("/v1/structure/cnc", json=_cnc_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "cnc_blocks" in body
    assert "ruest_chunks" in body
    assert "material_chunks" in body


@pytest.mark.anyio
async def test_cnc_endpoint_cnc_block_fields(client):
    md = "N10 G01 X100 F0.3 S1200\nN20 G01 Y50 F0.2 S1000"
    resp = await client.post("/v1/structure/cnc", json=_cnc_body(markdown=md))
    body = resp.json()
    cnc = body["cnc_blocks"]
    assert len(cnc) >= 1
    block = cnc[0]
    assert "cnc_step_id" in block
    assert block["operation_type"] == "Linearfraesen"
    assert block["cutting_speed"] == 1200


@pytest.mark.anyio
async def test_cnc_endpoint_material_chunks(client):
    md = "Material: Edelstahl, Härte 58 HRC, Dichte hoch. " * 50
    resp = await client.post("/v1/structure/cnc", json=_cnc_body(markdown=md))
    body = resp.json()
    assert len(body["material_chunks"]) >= 1


@pytest.mark.anyio
async def test_cnc_endpoint_ruest_chunks(client):
    md = "Allgemeine Rüstanweisung für Werkzeug A. " * 20
    resp = await client.post("/v1/structure/cnc", json=_cnc_body(markdown=md))
    body = resp.json()
    assert len(body["ruest_chunks"]) >= 1


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

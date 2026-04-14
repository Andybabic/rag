"""Tests for vectordb API endpoints using in-memory Qdrant."""

from __future__ import annotations

import httpx
import pytest
from main import app

DIM = 4


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _upsert_body(collection: str = "test_col", n: int = 10, **meta_overrides):
    return {
        "collection": collection,
        "embeddings": [
            {
                "chunk_id": f"id-{i}",
                "vector": [(i + 1) * 0.1] * DIM,
                "metadata": {
                    "text": f"chunk {i}",
                    "file_name": "doc.pdf",
                    **meta_overrides,
                },
            }
            for i in range(n)
        ],
    }


def _search_body(collection: str = "test_col", top_k: int = 5, **kwargs):
    body = {
        "collection": collection,
        "vector": [0.5] * DIM,
        "top_k": top_k,
    }
    body.update(kwargs)
    return body


# ── POST /v1/upsert ─────────────────────────────────────────


@pytest.mark.anyio
async def test_upsert_basic(client):
    resp = await client.post("/v1/upsert", json=_upsert_body(n=10))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["upserted"] == 10
    assert body["collection"] == "test_col"


@pytest.mark.anyio
async def test_upsert_auto_creates_collection(client):
    await client.post("/v1/upsert", json=_upsert_body(collection="new_col", n=3))
    resp = await client.get("/v1/collections")
    names = [c["name"] for c in resp.json()["collections"]]
    assert "new_col" in names


# ── POST /v1/search ──────────────────────────────────────────


@pytest.mark.anyio
async def test_search_basic(client):
    await client.post("/v1/upsert", json=_upsert_body(n=10))
    resp = await client.post("/v1/search", json=_search_body(top_k=5))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["results"]) == 5
    assert body["collection"] == "test_col"


@pytest.mark.anyio
async def test_search_top_k_limits(client):
    await client.post("/v1/upsert", json=_upsert_body(n=20))
    resp = await client.post("/v1/search", json=_search_body(top_k=3))
    body = resp.json()
    assert body["total"] == 3


@pytest.mark.anyio
async def test_search_sorted_by_score(client):
    await client.post("/v1/upsert", json=_upsert_body(n=10))
    resp = await client.post("/v1/search", json=_search_body(top_k=10))
    scores = [r["score"] for r in resp.json()["results"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.anyio
async def test_search_with_filter(client):
    # Insert 5 with M-4711, 5 with M-9999
    body_4711 = _upsert_body(n=5, machine_id="M-4711")
    body_9999 = {
        "collection": "test_col",
        "embeddings": [
            {
                "chunk_id": f"other-{i}",
                "vector": [0.9] * DIM,
                "metadata": {"text": "other", "machine_id": "M-9999"},
            }
            for i in range(5)
        ],
    }
    await client.post("/v1/upsert", json=body_4711)
    await client.post("/v1/upsert", json=body_9999)

    resp = await client.post(
        "/v1/search",
        json=_search_body(top_k=10, filters={"machine_id": "M-4711"}),
    )
    body = resp.json()
    assert body["total"] == 5
    assert all(r["metadata"]["machine_id"] == "M-4711" for r in body["results"])


@pytest.mark.anyio
async def test_search_nonexistent_collection(client):
    resp = await client.post(
        "/v1/search",
        json=_search_body(collection="nonexistent"),
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_search_result_fields(client):
    await client.post("/v1/upsert", json=_upsert_body(n=3))
    resp = await client.post("/v1/search", json=_search_body(top_k=1))
    result = resp.json()["results"][0]
    assert "chunk_id" in result
    assert "score" in result
    assert "text" in result
    assert "metadata" in result


# ── GET /v1/collections ──────────────────────────────────────


@pytest.mark.anyio
async def test_collections_empty(client):
    resp = await client.get("/v1/collections")
    assert resp.status_code == 200
    assert resp.json()["collections"] == []


@pytest.mark.anyio
async def test_collections_with_data(client):
    await client.post("/v1/upsert", json=_upsert_body(collection="col_a", n=5))
    await client.post("/v1/upsert", json=_upsert_body(collection="col_b", n=8))
    resp = await client.get("/v1/collections")
    body = resp.json()
    names = {c["name"] for c in body["collections"]}
    assert "col_a" in names
    assert "col_b" in names
    col_a = next(c for c in body["collections"] if c["name"] == "col_a")
    assert col_a["count"] == 5
    assert col_a["dimension"] == DIM


# ── DELETE /v1/collection/{name} ─────────────────────────────


@pytest.mark.anyio
async def test_delete_collection(client):
    await client.post("/v1/upsert", json=_upsert_body(collection="to_delete", n=2))
    resp = await client.delete("/v1/collection/to_delete")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["deleted"] == "to_delete"
    # Verify it's gone
    cols = await client.get("/v1/collections")
    names = {c["name"] for c in cols.json()["collections"]}
    assert "to_delete" not in names


@pytest.mark.anyio
async def test_delete_nonexistent_collection(client):
    resp = await client.delete("/v1/collection/nonexistent")
    assert resp.status_code == 404


# ── POST /v1/search/cross ───────────────────────────────────


async def _setup_cross_data(client):
    """Insert primary + linked collections for cross-search tests."""
    # Primary CNC steps
    await client.post("/v1/upsert", json={
        "collection": "gw_cnc_steps",
        "embeddings": [
            {
                "chunk_id": f"cnc-{i}",
                "vector": [(i + 1) * 0.1] * DIM,
                "metadata": {"text": f"CNC step {i}", "cnc_step_id": f"step-{i}"},
            }
            for i in range(5)
        ],
    })
    # Linked ruest_data (3 of 5 have matches)
    await client.post("/v1/upsert", json={
        "collection": "gw_ruest_data",
        "embeddings": [
            {
                "chunk_id": f"ruest-{i}",
                "vector": [(i + 1) * 0.1] * DIM,
                "metadata": {
                    "text": f"Rüstdaten für step-{i}",
                    "cnc_step_id": f"step-{i}",
                },
            }
            for i in range(3)
        ],
    })
    # Linked material_info (2 of 5 have matches)
    await client.post("/v1/upsert", json={
        "collection": "gw_material_info",
        "embeddings": [
            {
                "chunk_id": f"mat-{i}",
                "vector": [(i + 1) * 0.1] * DIM,
                "metadata": {
                    "text": f"Material info for step-{i}",
                    "cnc_step_id": f"step-{i}",
                },
            }
            for i in range(2)
        ],
    })


@pytest.mark.anyio
async def test_cross_search_basic(client):
    await _setup_cross_data(client)
    resp = await client.post("/v1/search/cross", json={
        "primary_collection": "gw_cnc_steps",
        "linked_collections": ["gw_ruest_data", "gw_material_info"],
        "vector": [0.3] * DIM,
        "link_key": "cnc_step_id",
        "top_k": 3,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["results"]) == 3
    for r in body["results"]:
        assert "primary" in r
        assert "linked" in r
        assert "gw_ruest_data" in r["linked"]
        assert "gw_material_info" in r["linked"]


@pytest.mark.anyio
async def test_cross_search_linked_null_when_no_match(client):
    await _setup_cross_data(client)
    resp = await client.post("/v1/search/cross", json={
        "primary_collection": "gw_cnc_steps",
        "linked_collections": ["gw_ruest_data", "gw_material_info"],
        "vector": [0.5] * DIM,
        "link_key": "cnc_step_id",
        "top_k": 5,
    })
    body = resp.json()
    # Some results won't have matching linked data
    has_null = any(
        r["linked"]["gw_material_info"] is None or r["linked"]["gw_ruest_data"] is None
        for r in body["results"]
    )
    assert has_null


@pytest.mark.anyio
async def test_cross_search_primary_not_found(client):
    resp = await client.post("/v1/search/cross", json={
        "primary_collection": "nonexistent",
        "linked_collections": ["gw_ruest_data"],
        "vector": [0.5] * DIM,
        "link_key": "cnc_step_id",
        "top_k": 3,
    })
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_cross_search_primary_fields(client):
    await _setup_cross_data(client)
    resp = await client.post("/v1/search/cross", json={
        "primary_collection": "gw_cnc_steps",
        "linked_collections": ["gw_ruest_data"],
        "vector": [0.3] * DIM,
        "link_key": "cnc_step_id",
        "top_k": 1,
    })
    body = resp.json()
    primary = body["results"][0]["primary"]
    assert "chunk_id" in primary
    assert "score" in primary
    assert "text" in primary
    assert "metadata" in primary

"""Use-Case-Transfer: Collections samt Vektoren aus- und wieder einlesen."""

from __future__ import annotations

import httpx
import pytest
from main import app

DIM = 4


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _seed(client, collection: str, n: int) -> None:
    await client.post("/v1/upsert", json={
        "collection": collection,
        "embed_model": "bge-m3",
        "embeddings": [
            {"chunk_id": f"id-{i}", "vector": [(i + 1) * 0.1] * DIM,
             "metadata": {"text": f"chunk {i}", "use_case": "alt"}}
            for i in range(n)
        ],
    })


async def _export_all(client, collection: str, limit: int) -> tuple[int, list[dict]]:
    points: list[dict] = []
    offset = None
    dimension = 0
    while True:
        resp = await client.post("/v1/points/export", json={
            "collection": collection, "offset": offset, "limit": limit,
        })
        assert resp.status_code == 200
        page = resp.json()
        dimension = page["dimension"]
        points += page["points"]
        offset = page["next"]
        if offset is None:
            return dimension, points


@pytest.mark.anyio
async def test_export_pages_through_every_point_with_vectors(client):
    await _seed(client, "src", 7)
    dimension, points = await _export_all(client, "src", limit=3)
    assert dimension == DIM
    assert len(points) == 7
    assert len({p["id"] for p in points}) == 7
    assert all(len(p["vector"]) == DIM for p in points)
    # The model stamp travels with the payload — the import side checks it.
    assert all(p["payload"]["_embed_model"] == "bge-m3" for p in points)


@pytest.mark.anyio
async def test_import_rebuilds_the_collection_unchanged(client):
    await _seed(client, "src", 5)
    dimension, points = await _export_all(client, "src", limit=100)

    resp = await client.post("/v1/points/import", json={
        "collection": "dst", "dimension": dimension, "points": points[:2], "create": True,
    })
    assert resp.status_code == 200 and resp.json()["imported"] == 2
    resp = await client.post("/v1/points/import", json={
        "collection": "dst", "dimension": dimension, "points": points[2:],
    })
    assert resp.json()["imported"] == 3

    _, copied = await _export_all(client, "dst", limit=100)
    by_id = {p["id"]: p for p in copied}
    for original in points:
        assert by_id[original["id"]]["payload"] == original["payload"]
        assert by_id[original["id"]]["vector"] == pytest.approx(original["vector"])


@pytest.mark.anyio
async def test_import_never_writes_into_an_existing_collection(client):
    await _seed(client, "taken", 2)
    _, points = await _export_all(client, "taken", limit=100)
    resp = await client.post("/v1/points/import", json={
        "collection": "taken", "dimension": DIM, "points": points, "create": True,
    })
    assert resp.status_code == 409
    assert resp.json()["error"] == "collection_exists"


@pytest.mark.anyio
async def test_follow_up_page_needs_the_collection(client):
    resp = await client.post("/v1/points/import", json={
        "collection": "missing", "dimension": DIM, "points": [],
    })
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_export_of_unknown_collection_is_404(client):
    resp = await client.post("/v1/points/export", json={"collection": "nope"})
    assert resp.status_code == 404

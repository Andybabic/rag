"""Exakte Textsuche (Strg+F) nach Codes über Collections."""

from __future__ import annotations

import httpx
import pytest
from main import app

DIM = 4


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _seed(client, collection: str, texts: list[str]) -> None:
    await client.post("/v1/upsert", json={
        "collection": collection,
        "embed_model": "bge-m3",
        "embeddings": [
            {"chunk_id": f"{collection}-{i}", "vector": [(i + 1) * 0.1] * DIM,
             "metadata": {"text": text, "file_name": f"{collection}.pdf", "page": i}}
            for i, text in enumerate(texts)
        ],
    })


async def _find(client, collections, terms, limit=200) -> dict:
    resp = await client.post("/v1/points/find", json={
        "collections": collections, "terms": terms, "limit": limit,
    })
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.anyio
async def test_finds_the_code_in_every_collection_with_its_source(client):
    await _seed(client, "a", ["Warnung A30042 ausgegeben", "etwas anderes"])
    await _seed(client, "b", ["Fehler A30042 im Leistungsteil"])
    found = await _find(client, ["a", "b"], ["A30042"])
    hits = found["hits"]["A30042"]
    assert {h["collection"] for h in hits} == {"a", "b"}
    assert {h["metadata"]["file_name"] for h in hits} == {"a.pdf", "b.pdf"}
    assert all("A30042" in h["text"] for h in hits)


@pytest.mark.anyio
async def test_matches_whole_token_and_exact_case_only(client):
    await _seed(client, "a", ["Warnung A300421 ausgegeben", "warnung a30042 klein"])
    found = await _find(client, ["a"], ["A30042"])
    assert found["hits"]["A30042"] == []


@pytest.mark.anyio
async def test_each_term_has_its_own_hits(client):
    await _seed(client, "a", ["Zähler p0251 auf 0", "Warnung A30042"])
    found = await _find(client, ["a"], ["p0251", "A30042", "F99999"])
    assert len(found["hits"]["p0251"]) == 1
    assert len(found["hits"]["A30042"]) == 1
    assert found["hits"]["F99999"] == []


@pytest.mark.anyio
async def test_unknown_collection_is_skipped(client):
    await _seed(client, "a", ["Warnung A30042"])
    found = await _find(client, ["fehlt", "a"], ["A30042"])
    assert len(found["hits"]["A30042"]) == 1


@pytest.mark.anyio
async def test_limit_truncates_and_says_so(client):
    await _seed(client, "a", [f"Warnung A30042 Nr {i}" for i in range(5)])
    found = await _find(client, ["a"], ["A30042"], limit=3)
    assert len(found["hits"]["A30042"]) == 3
    assert found["truncated"]["A30042"] is True

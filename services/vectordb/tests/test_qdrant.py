"""Tests for the Qdrant client wrapper using in-memory Qdrant."""

from __future__ import annotations

import pytest
from core.qdrant import (
    cross_search,
    delete_collection,
    get_client,
    list_collections,
    search_vectors,
    upsert_vectors,
)

DIM = 4


def _make_embeddings(n: int, **meta_overrides) -> list[dict]:
    """Create n fake embeddings with dimension DIM."""
    embeddings = []
    for i in range(n):
        meta = {"text": f"chunk text {i}", "file_name": "doc.pdf", **meta_overrides}
        embeddings.append({
            "chunk_id": f"id-{i}",
            "vector": [(i + 1) * 0.1] * DIM,
            "metadata": meta,
        })
    return embeddings


# ── Upsert ───────────────────────────────────────────────────


def test_upsert_creates_collection_and_inserts():
    """Auto-create collection on first upsert."""
    embs = _make_embeddings(10)
    count = upsert_vectors("test_col", embs)
    assert count == 10
    # Verify collection was created
    client = get_client()
    collections = {c.name for c in client.get_collections().collections}
    assert "test_col" in collections


def test_upsert_empty_list():
    count = upsert_vectors("test_col", [])
    assert count == 0


def test_upsert_points_retrievable():
    upsert_vectors("test_col", _make_embeddings(5))
    info = get_client().get_collection("test_col")
    assert info.points_count == 5


def test_upsert_idempotent():
    """Upserting the same IDs again overwrites, doesn't duplicate."""
    upsert_vectors("test_col", _make_embeddings(3))
    upsert_vectors("test_col", _make_embeddings(3))
    info = get_client().get_collection("test_col")
    assert info.points_count == 3


# ── Search ───────────────────────────────────────────────────


def test_search_returns_results():
    upsert_vectors("test_col", _make_embeddings(10))
    results = search_vectors("test_col", [0.5] * DIM, top_k=5)
    assert len(results) == 5
    assert all("chunk_id" in r for r in results)
    assert all("score" in r for r in results)
    assert all("text" in r for r in results)
    assert all("metadata" in r for r in results)


def test_search_top_k_limits_results():
    upsert_vectors("test_col", _make_embeddings(20))
    results = search_vectors("test_col", [0.5] * DIM, top_k=3)
    assert len(results) == 3


def test_search_sorted_by_score():
    upsert_vectors("test_col", _make_embeddings(10))
    results = search_vectors("test_col", [0.5] * DIM, top_k=10)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_search_with_filter():
    embs = _make_embeddings(5, machine_id="M-4711")
    embs += [
        {
            "chunk_id": "other-1",
            "vector": [0.9] * DIM,
            "metadata": {"text": "other", "machine_id": "M-9999"},
        }
    ]
    upsert_vectors("test_col", embs)
    results = search_vectors(
        "test_col",
        [0.5] * DIM,
        top_k=10,
        filters={"machine_id": "M-4711"},
    )
    assert len(results) == 5
    assert all(r["metadata"]["machine_id"] == "M-4711" for r in results)


def test_search_nonexistent_collection():
    with pytest.raises(ValueError, match="not found"):
        search_vectors("nonexistent", [0.5] * DIM)


# ── Collections ──────────────────────────────────────────────


def test_list_collections_empty():
    result = list_collections()
    assert result == []


def test_list_collections_with_data():
    upsert_vectors("col_a", _make_embeddings(3))
    upsert_vectors("col_b", _make_embeddings(7))
    result = list_collections()
    names = {c["name"] for c in result}
    assert "col_a" in names
    assert "col_b" in names
    col_a = next(c for c in result if c["name"] == "col_a")
    assert col_a["count"] == 3
    assert col_a["dimension"] == DIM


def test_delete_collection():
    upsert_vectors("to_delete", _make_embeddings(2))
    delete_collection("to_delete")
    result = list_collections()
    names = {c["name"] for c in result}
    assert "to_delete" not in names


def test_delete_nonexistent_collection():
    with pytest.raises(ValueError, match="not found"):
        delete_collection("nonexistent")


# ── Cross-Search ─────────────────────────────────────────────


def _setup_cross_search_data():
    """Set up primary + linked collections for cross-search tests."""
    # Primary: CNC steps with cnc_step_id in metadata
    primary = [
        {
            "chunk_id": f"cnc-{i}",
            "vector": [(i + 1) * 0.1] * DIM,
            "metadata": {"text": f"CNC step {i}", "cnc_step_id": f"step-{i}"},
        }
        for i in range(5)
    ]
    upsert_vectors("gw_cnc_steps", primary)

    # Linked: ruest_data with matching cnc_step_id
    ruest = [
        {
            "chunk_id": f"ruest-{i}",
            "vector": [(i + 1) * 0.1] * DIM,
            "metadata": {"text": f"Rüstdaten {i}", "cnc_step_id": f"step-{i}"},
        }
        for i in range(3)  # only 3 of 5 have linked data
    ]
    upsert_vectors("gw_ruest_data", ruest)

    # Linked: material_info with matching cnc_step_id
    material = [
        {
            "chunk_id": f"mat-{i}",
            "vector": [(i + 1) * 0.1] * DIM,
            "metadata": {"text": f"Material {i}", "cnc_step_id": f"step-{i}"},
        }
        for i in range(2)  # only 2 of 5 have linked data
    ]
    upsert_vectors("gw_material_info", material)


def test_cross_search_returns_primary_and_linked():
    _setup_cross_search_data()
    results = cross_search(
        primary_collection="gw_cnc_steps",
        linked_collections=["gw_ruest_data", "gw_material_info"],
        vector=[0.3] * DIM,
        link_key="cnc_step_id",
        top_k=3,
    )
    assert len(results) == 3
    for r in results:
        assert "primary" in r
        assert "linked" in r
        assert "gw_ruest_data" in r["linked"]
        assert "gw_material_info" in r["linked"]


def test_cross_search_linked_null_when_no_match():
    _setup_cross_search_data()
    results = cross_search(
        primary_collection="gw_cnc_steps",
        linked_collections=["gw_ruest_data", "gw_material_info"],
        vector=[0.5] * DIM,
        link_key="cnc_step_id",
        top_k=5,
    )
    # Some primary results won't have matching linked data
    has_null = any(
        r["linked"]["gw_material_info"] is None or r["linked"]["gw_ruest_data"] is None
        for r in results
    )
    assert has_null


def test_cross_search_nonexistent_linked_collection():
    """Missing linked collection → linked[col] = None, no error."""
    upsert_vectors(
        "primary_only",
        [{"chunk_id": "p-0", "vector": [0.5] * DIM, "metadata": {"link": "val"}}],
    )
    results = cross_search(
        primary_collection="primary_only",
        linked_collections=["nonexistent_col"],
        vector=[0.5] * DIM,
        link_key="link",
        top_k=1,
    )
    assert len(results) == 1
    assert results[0]["linked"]["nonexistent_col"] is None

"""Tests for the evaluator module."""

from __future__ import annotations

from core.evaluator import evaluate_chunks


def _chunks(scores: list[float]) -> list[dict]:
    return [
        {"chunk_id": f"id-{i}", "text": f"text {i}", "score": s, "metadata": {}}
        for i, s in enumerate(scores)
    ]


def test_evaluate_sufficient():
    result = evaluate_chunks("query", _chunks([0.8, 0.6, 0.7]), min_chunks=2, min_score=0.5)
    assert result["sufficient"] is True
    assert result["recommended_action"] == "proceed"


def test_evaluate_insufficient_too_few():
    result = evaluate_chunks("query", _chunks([0.8, 0.3, 0.2]), min_chunks=2, min_score=0.5)
    assert result["sufficient"] is False
    assert "1 Chunk" in result["reason"]
    assert "mindestens 2" in result["reason"]
    assert result["recommended_action"] == "refine_query"


def test_evaluate_insufficient_none_above():
    result = evaluate_chunks("query", _chunks([0.1, 0.2, 0.3]), min_chunks=1, min_score=0.5)
    assert result["sufficient"] is False
    assert "0 Chunks" in result["reason"]


def test_evaluate_exact_threshold():
    result = evaluate_chunks("query", _chunks([0.5, 0.5]), min_chunks=2, min_score=0.5)
    assert result["sufficient"] is True


def test_evaluate_hint_contains_query_words():
    result = evaluate_chunks(
        "Hydraulikdruck Wartung", _chunks([0.3]), min_chunks=2, min_score=0.5
    )
    assert result["refined_query_hint"] is not None
    assert "Hydraulikdruck" in result["refined_query_hint"]


def test_evaluate_hint_includes_metadata():
    chunks = [
        {"chunk_id": "id-0", "text": "text", "score": 0.3, "metadata": {"topic": "wartung"}}
    ]
    result = evaluate_chunks("query", chunks, min_chunks=2, min_score=0.5)
    assert "wartung" in result["refined_query_hint"]


def test_evaluate_empty_chunks():
    result = evaluate_chunks("query", [], min_chunks=1, min_score=0.5)
    assert result["sufficient"] is False

"""Tests for the reranker module.

These tests exercise the fallback path (no cross-encoder model available) so
they run fast and deterministically in CI. The fallback preserves input order
and copies scores through — that's the contract to test here.
"""

from __future__ import annotations

import core.reranker as reranker_mod
from core.reranker import rerank_chunks


def _chunks(scores: list[float]) -> list[dict]:
    return [
        {"chunk_id": f"id-{i}", "text": f"text {i}", "score": s, "metadata": {}}
        for i, s in enumerate(scores)
    ]


def _force_fallback(monkeypatch) -> None:
    monkeypatch.setattr(reranker_mod, "_model", None)
    monkeypatch.setattr(reranker_mod, "_model_load_failed", True)


def test_rerank_fallback_preserves_order(monkeypatch):
    _force_fallback(monkeypatch)
    chunks = _chunks([0.3, 0.9, 0.5, 0.7])
    out = rerank_chunks("q", chunks, top_n=4)
    assert [c["chunk_id"] for c in out] == ["id-0", "id-1", "id-2", "id-3"]


def test_rerank_respects_top_n(monkeypatch):
    _force_fallback(monkeypatch)
    chunks = _chunks([0.1, 0.4, 0.6, 0.8])
    out = rerank_chunks("q", chunks, top_n=2)
    assert len(out) == 2


def test_rerank_preserves_original_score(monkeypatch):
    _force_fallback(monkeypatch)
    chunks = _chunks([0.72])
    out = rerank_chunks("q", chunks, top_n=1)
    assert out[0]["original_score"] == 0.72


def test_rerank_empty_input(monkeypatch):
    _force_fallback(monkeypatch)
    assert rerank_chunks("q", [], top_n=5) == []


def test_rerank_label_thresholds(monkeypatch):
    _force_fallback(monkeypatch)
    # Fallback uses incoming rerank_score if present, else score.
    chunks = [
        {"chunk_id": "hi", "metadata": {}, "rerank_score": 5.0, "score": 0.0},
        {"chunk_id": "ok", "metadata": {}, "rerank_score": 1.0, "score": 0.0},
        {"chunk_id": "meh", "metadata": {}, "rerank_score": -1.0, "score": 0.0},
        {"chunk_id": "no", "metadata": {}, "rerank_score": -5.0, "score": 0.0},
    ]
    out = rerank_chunks("q", chunks, top_n=4)
    labels = {c["chunk_id"]: c["relevance_label"] for c in out}
    assert labels == {
        "hi": "highly_relevant",
        "ok": "relevant",
        "meh": "marginally_relevant",
        "no": "not_relevant",
    }

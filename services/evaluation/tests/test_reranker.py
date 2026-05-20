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


def test_fallback_labels_are_unranked(monkeypatch):
    """Without the cross-encoder the fallback must NOT fake cross-encoder
    relevance labels (their thresholds are tuned for logits, not the tiny
    cosine/RRF scores here) – that mislabelling fooled the agent into
    trusting near-zero matches. Fallback must report 'unranked'."""
    _force_fallback(monkeypatch)
    chunks = [
        {"chunk_id": "hi", "metadata": {}, "rerank_score": 5.0, "score": 0.0},
        {"chunk_id": "lo", "metadata": {}, "rerank_score": 0.0003, "score": 0.0},
    ]
    out = rerank_chunks("q", chunks, top_n=2)
    assert {c["relevance_label"] for c in out} == {"unranked"}
    # Scores are still passed through for ordering/inspection.
    assert out[0]["rerank_score"] == 5.0


def test_relevance_label_thresholds():
    """The threshold mapping itself is still used by the real cross-encoder
    path and must keep its logit-based boundaries."""
    from core.reranker import _relevance_label

    assert _relevance_label(5.0) == "highly_relevant"
    assert _relevance_label(1.0) == "relevant"
    assert _relevance_label(-1.0) == "marginally_relevant"
    assert _relevance_label(-5.0) == "not_relevant"

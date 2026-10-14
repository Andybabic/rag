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


def test_backend_is_local_unless_api_requested(monkeypatch):
    """Qwen3-Reranker loads through CrossEncoder like bge; only an explicit
    RERANKER_TYPE=api switches to the remote backend."""
    monkeypatch.setattr(reranker_mod, "_RERANKER_TYPE", "auto")
    assert reranker_mod._resolved_type() == "cross_encoder"
    monkeypatch.setattr(reranker_mod, "_RERANKER_TYPE", "api")
    assert reranker_mod._resolved_type() == "api"


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _api_model(monkeypatch, payload: dict, sent: list) -> object:
    def fake_post(url, json, headers, timeout):
        sent.append({"url": url, "json": json})
        return _FakeResponse(payload)

    monkeypatch.setattr(reranker_mod.httpx, "post", fake_post)
    return reranker_mod._ApiReranker("http://spark:8000/v1", "BAAI/bge-reranker-v2-m3")


def test_api_backend_maps_probabilities_to_logits(monkeypatch):
    """The API returns results sorted by score and as probabilities; scores
    must land at their input position and on the logit scale of the labels."""
    sent: list = []
    model = _api_model(
        monkeypatch,
        {"results": [{"index": 1, "relevance_score": 0.99},
                     {"index": 0, "relevance_score": 0.01}]},
        sent,
    )
    scores = model.predict([("q", "a"), ("q", "b")])
    assert sent[0]["url"] == "http://spark:8000/v1/rerank"
    assert sent[0]["json"]["documents"] == ["a", "b"]
    assert reranker_mod._relevance_label(scores[1]) == "highly_relevant"
    assert reranker_mod._relevance_label(scores[0]) == "not_relevant"


def test_api_backend_wraps_qwen3_prompt(monkeypatch):
    sent: list = []
    monkeypatch.setattr(reranker_mod.httpx, "post", lambda url, json, headers, timeout: (
        sent.append(json) or _FakeResponse({"results": [{"index": 0, "relevance_score": 0.5}]})
    ))
    model = reranker_mod._ApiReranker("http://spark:8000/v1", "Qwen/Qwen3-Reranker-0.6B")
    model.predict([("Frage", "Text")])
    assert "<Query>: Frage" in sent[0]["query"]
    assert sent[0]["documents"][0].startswith("<Document>: Text")


def test_api_backend_rejects_partial_results(monkeypatch):
    model = _api_model(
        monkeypatch, {"results": [{"index": 0, "relevance_score": 0.9}]}, []
    )
    try:
        model.predict([("q", "a"), ("q", "b")])
    except ValueError:
        return
    raise AssertionError("partial rerank result must raise")


def test_predict_failure_falls_back_to_unranked(monkeypatch):
    """A failing reranker (e.g. API unreachable) must not label fused RRF
    scores with the logit thresholds – that would call everything relevant."""
    class Broken:
        def predict(self, pairs, show_progress_bar=False):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(reranker_mod, "_RERANKER_ENABLED", True)
    monkeypatch.setattr(reranker_mod, "_model", Broken())
    chunks = _chunks([0.03, 0.02])
    out = rerank_chunks("q", chunks, top_n=2)
    assert [c["chunk_id"] for c in out] == ["id-0", "id-1"]
    assert {c["relevance_label"] for c in out} == {"unranked"}


def test_api_backend_reports_server_error_message(monkeypatch):
    """A 404 must carry vLLM's own reason, e.g. an unknown model name."""
    payload = {"error": {"message": "The model `Qwen/Qwen3-Reranker-4B` does not exist."}}
    monkeypatch.setattr(reranker_mod.httpx, "post",
                        lambda url, json, headers, timeout: _FakeResponse(payload, 404))
    model = reranker_mod._ApiReranker("http://spark:8000/v1", "Qwen/Qwen3-Reranker-4B")
    try:
        model.predict([("q", "a")])
    except RuntimeError as exc:
        assert "does not exist" in str(exc)
        return
    raise AssertionError("HTTP 404 must raise")

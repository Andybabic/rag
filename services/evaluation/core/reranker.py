"""Reranking – cross-encoder (bge-reranker-v2-m3) with a safe fallback.

The cross-encoder scores each (query, chunk) pair jointly, which is far more
discriminating than cosine similarity between separate embeddings. This is the
biggest quality lever once the vector space is dense. Model is lazy-loaded and
cached; if loading fails (no model on disk, no torch, etc.), we fall back to
the existing fused-score ordering so the service never hard-fails on retrieval.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_RERANKER_MODEL_NAME = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
_RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() in ("1", "true", "yes")

_model = None
_model_lock = threading.Lock()
_model_load_failed = False
_model_loading = False  # True while the background loader thread is running


def _relevance_label(score: float) -> str:
    """Map a score to a human-readable relevance label (cross-encoder scores
    are typically logits roughly in [-10, 10]; thresholds tuned for bge-reranker)."""
    if score >= 3.0:
        return "highly_relevant"
    if score >= 0.0:
        return "relevant"
    if score >= -3.0:
        return "marginally_relevant"
    return "not_relevant"


def _load_model_blocking() -> None:
    """Actually load the model. Runs in a background thread."""
    global _model, _model_load_failed, _model_loading  # noqa: PLW0603
    try:
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(_RERANKER_MODEL_NAME, max_length=512)
        with _model_lock:
            _model = model
        logger.info("Loaded reranker model: %s", _RERANKER_MODEL_NAME)
    except Exception as exc:  # noqa: BLE001
        with _model_lock:
            _model_load_failed = True
        logger.warning(
            "Reranker unavailable (%s); using score-based fallback", exc
        )
    finally:
        with _model_lock:
            _model_loading = False


def preload_model() -> None:
    """Kick off the model download/load in a background thread.

    Call this once at service startup. Until the model is ready, requests fall
    back to the hybrid-fusion ordering instead of blocking the event loop.
    """
    global _model_loading  # noqa: PLW0603
    if not _RERANKER_ENABLED:
        logger.info("Reranker disabled via RERANKER_ENABLED=false")
        return
    with _model_lock:
        if _model is not None or _model_load_failed or _model_loading:
            return
        _model_loading = True
    logger.info("Preloading reranker model in background: %s", _RERANKER_MODEL_NAME)
    threading.Thread(
        target=_load_model_blocking, name="reranker-loader", daemon=True
    ).start()


def _get_model():
    """Return the loaded cross-encoder, or None if not ready / unavailable.

    Non-blocking: if the model is still downloading, this returns None and the
    caller uses the fallback path. As soon as the background loader finishes,
    subsequent calls will start using the real model.
    """
    if not _RERANKER_ENABLED:
        return None
    with _model_lock:
        if _model is not None:
            return _model
        if _model_load_failed or _model_loading:
            return None
        # Not preloaded (e.g. import from a script) – kick off loader once.
    preload_model()
    return None


def _chunk_text(chunk: dict) -> str:
    meta = chunk.get("metadata", {}) or {}
    return meta.get("text") or chunk.get("text", "") or ""


def rerank_chunks(
    query: str,
    chunks: list[dict],
    *,
    top_n: int = 10,
) -> list[dict]:
    """Return the top-n chunks re-ranked by a cross-encoder against the query.

    Each returned chunk has `rerank_score` (cross-encoder logit) and
    `relevance_label`. If the cross-encoder can't load, we preserve the input
    ordering (which is already the hybrid-fused ranking) and copy scores.
    """
    if not chunks:
        return []

    model = _get_model()
    if model is None:
        # Fallback: keep the incoming order, re-shape fields
        out = []
        for c in chunks[:top_n]:
            score = c.get("rerank_score", c.get("score", 0.0))
            out.append({
                "chunk_id": c.get("chunk_id")
                or (c.get("metadata") or {}).get("chunk_id", ""),
                "original_score": c.get("original_score", c.get("score", 0.0)),
                "rerank_score": score,
                "relevance_label": _relevance_label(score),
                "metadata": c.get("metadata", {}),
            })
        return out

    pairs = [(query, _chunk_text(c)) for c in chunks]
    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reranker predict() failed (%s); using fused scores", exc)
        scores = [c.get("rerank_score", c.get("score", 0.0)) for c in chunks]

    scored = []
    for chunk, score in zip(chunks, scores):
        score = float(score)
        scored.append({
            "chunk_id": chunk.get("chunk_id")
            or (chunk.get("metadata") or {}).get("chunk_id", ""),
            "original_score": chunk.get("original_score", chunk.get("score", 0.0)),
            "rerank_score": score,
            "relevance_label": _relevance_label(score),
            "metadata": chunk.get("metadata", {}),
        })

    scored.sort(key=lambda x: x["rerank_score"], reverse=True)
    return scored[:top_n]

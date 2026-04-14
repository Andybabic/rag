"""Hybrid retrieval: combine dense (vector) and sparse (BM25) scores via RRF.

We fetch a larger candidate pool from Qdrant, compute BM25 locally over the
candidates' text payloads against the query, then fuse both rankings with
Reciprocal Rank Fusion. This catches exact-keyword hits (names, IDs, rare
terms) that dense embeddings tend to wash out in a dense vector space.
"""

from __future__ import annotations

import re
from collections import defaultdict

from rank_bm25 import BM25Okapi

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _chunk_text(candidate: dict) -> str:
    """Return the searchable text for a candidate, regardless of payload shape."""
    meta = candidate.get("metadata", {}) or {}
    return meta.get("text") or candidate.get("text", "") or ""


def bm25_scores(query: str, candidates: list[dict]) -> list[float]:
    """Return BM25 score per candidate (same order as input)."""
    if not candidates:
        return []
    corpus = [_tokenize(_chunk_text(c)) for c in candidates]
    if not any(corpus):
        return [0.0] * len(candidates)
    bm25 = BM25Okapi(corpus)
    return list(bm25.get_scores(_tokenize(query)))


def reciprocal_rank_fusion(
    rankings: list[list[str]],
    *,
    k: int = 60,
) -> dict[str, float]:
    """Fuse multiple ranked id-lists into a single score map via RRF.

    score(id) = sum(1 / (k + rank_i)) over every ranking the id appears in.
    Standard k=60 works well across most ranges; tuning rarely helps.
    """
    fused: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            fused[cid] += 1.0 / (k + rank + 1)
    return dict(fused)


def hybrid_fuse(query: str, candidates: list[dict]) -> list[dict]:
    """Rank candidates by RRF of (dense score, BM25 score).

    Each candidate must have a stable id (chunk_id preferred; falls back to
    text hash). Returns the list re-sorted with a 'rerank_score' field (the
    fused RRF score — not comparable to the raw vector cosine scores).
    """
    if not candidates:
        return []

    # Dense ranking: candidates are assumed pre-sorted by vector score desc
    # (Qdrant returns them that way). Fall back to explicit sort to be safe.
    dense_sorted = sorted(
        candidates,
        key=lambda c: c.get("score", 0.0),
        reverse=True,
    )
    dense_ranking = [_cid(c) for c in dense_sorted]

    # Sparse ranking via BM25 on the same candidate pool
    scores = bm25_scores(query, candidates)
    sparse_sorted = [
        c for _, c in sorted(zip(scores, candidates), key=lambda p: p[0], reverse=True)
    ]
    sparse_ranking = [_cid(c) for c in sparse_sorted]

    fused = reciprocal_rank_fusion([dense_ranking, sparse_ranking])

    by_id = {_cid(c): c for c in candidates}
    fused_sorted_ids = sorted(fused.keys(), key=lambda cid: fused[cid], reverse=True)

    out: list[dict] = []
    for cid in fused_sorted_ids:
        c = dict(by_id[cid])
        c["rerank_score"] = fused[cid]
        c["original_score"] = c.get("score", 0.0)
        out.append(c)
    return out


def _cid(candidate: dict) -> str:
    return (
        candidate.get("chunk_id")
        or (candidate.get("metadata") or {}).get("chunk_id")
        or str(id(candidate))
    )

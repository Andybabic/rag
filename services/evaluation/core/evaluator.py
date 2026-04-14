"""Evaluate whether search results are sufficient for answering a query."""

from __future__ import annotations


def evaluate_chunks(
    query: str,
    chunks: list[dict],
    min_chunks: int = 2,
    min_score: float = 0.5,
) -> dict:
    """Check if enough high-quality chunks exist to answer the query.

    Returns a dict with sufficient, reason, recommended_action,
    and refined_query_hint fields.
    """
    above = [c for c in chunks if c["score"] >= min_score]
    count = len(above)

    if count >= min_chunks:
        return {
            "sufficient": True,
            "reason": f"{count} Chunks über Threshold {min_score}",
            "recommended_action": "proceed",
            "refined_query_hint": None,
        }

    # Build a hint from query keywords + chunk metadata
    hint_parts = query.split()[:5]
    for c in chunks[:3]:
        meta = c.get("metadata", {})
        for key in ("topic", "area", "operation_type"):
            if key in meta and meta[key] not in hint_parts:
                hint_parts.append(meta[key])

    return {
        "sufficient": False,
        "reason": (
            f"Nur {count} Chunk{'s' if count != 1 else ''} über Threshold {min_score}, "
            f"mindestens {min_chunks} benötigt"
        ),
        "recommended_action": "refine_query",
        "refined_query_hint": " ".join(hint_parts),
    }

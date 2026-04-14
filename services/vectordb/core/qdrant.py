"""Qdrant client wrapper."""

from __future__ import annotations

from uuid import NAMESPACE_DNS, uuid5

from config import settings
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

_client: QdrantClient | None = None


def _to_uuid(chunk_id: str) -> str:
    """Convert an arbitrary chunk_id string to a valid UUID string."""
    from uuid import UUID

    try:
        UUID(chunk_id)
        return chunk_id
    except ValueError:
        return str(uuid5(NAMESPACE_DNS, chunk_id))


class QdrantUnavailableError(Exception):
    """Raised when Qdrant is not reachable."""


def get_client() -> QdrantClient:
    """Return the shared Qdrant client, creating it on first call."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = QdrantClient(
            host=settings.QDRANT_HOST,
            port=settings.QDRANT_PORT,
        )
    return _client


def set_client(client: QdrantClient | None) -> None:
    """Override the shared client (used in tests)."""
    global _client  # noqa: PLW0603
    _client = client


def _ensure_collection(client: QdrantClient, name: str, dimension: int) -> None:
    """Create a collection if it does not already exist."""
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
        )


def upsert_vectors(
    collection: str,
    embeddings: list[dict],
) -> int:
    """Upsert embedding points into a Qdrant collection.

    Auto-creates the collection if it does not exist.
    Returns the number of upserted points.
    """
    client = get_client()
    if not embeddings:
        return 0

    dimension = len(embeddings[0]["vector"])
    try:
        _ensure_collection(client, collection, dimension)
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant unreachable: {exc}") from exc

    points = [
        PointStruct(
            id=_to_uuid(emb["chunk_id"]),
            vector=emb["vector"],
            payload={**emb.get("metadata", {}), "chunk_id": emb["chunk_id"]},
        )
        for emb in embeddings
    ]

    try:
        client.upsert(collection_name=collection, points=points)
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant upsert failed: {exc}") from exc

    return len(points)


def _build_filter(filters: dict) -> Filter | None:
    """Convert a flat dict of filters to Qdrant Filter conditions."""
    if not filters:
        return None
    conditions = []
    for key, value in filters.items():
        conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
    return Filter(must=conditions)


def search_vectors(
    collection: str,
    vector: list[float],
    top_k: int = 5,
    filters: dict | None = None,
) -> list[dict]:
    """Search for similar vectors in a collection."""
    client = get_client()
    query_filter = _build_filter(filters or {})

    try:
        response = client.query_points(
            collection_name=collection,
            query=vector,
            limit=top_k,
            query_filter=query_filter,
        )
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise ValueError(f"Collection '{collection}' not found") from exc
        raise
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant search failed: {exc}") from exc

    return [
        {
            "chunk_id": r.payload.get("chunk_id", str(r.id)),
            "score": r.score,
            "text": r.payload.get("text", ""),
            "metadata": r.payload,
        }
        for r in response.points
    ]


def list_collections() -> list[dict]:
    """List all collections with point counts."""
    client = get_client()
    try:
        collections = client.get_collections().collections
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant unreachable: {exc}") from exc

    result = []
    for c in collections:
        info = client.get_collection(c.name)
        dimension = 0
        if info.config and info.config.params and info.config.params.vectors:
            dimension = info.config.params.vectors.size
        result.append({
            "name": c.name,
            "count": info.points_count or 0,
            "dimension": dimension,
        })
    return result


def cross_search(
    primary_collection: str,
    linked_collections: list[str],
    vector: list[float],
    link_key: str,
    top_k: int = 5,
    filters: dict | None = None,
) -> list[dict]:
    """Search primary collection, then look up linked data per result."""
    primary_results = search_vectors(
        collection=primary_collection,
        vector=vector,
        top_k=top_k,
        filters=filters,
    )

    combined = []
    for result in primary_results:
        link_value = result["metadata"].get(link_key)
        linked: dict[str, dict | None] = {}

        for col in linked_collections:
            if link_value is None:
                linked[col] = None
                continue
            try:
                hits = search_vectors(
                    collection=col,
                    vector=vector,
                    top_k=1,
                    filters={link_key: link_value},
                )
                linked[col] = hits[0] if hits else None
            except ValueError:
                linked[col] = None

        combined.append({"primary": result, "linked": linked})

    return combined


def delete_collection(name: str) -> bool:
    """Delete a collection. Returns True if deleted, raises ValueError if not found."""
    client = get_client()
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        raise ValueError(f"Collection '{name}' not found")
    try:
        client.delete_collection(collection_name=name)
        return True
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant delete failed: {exc}") from exc

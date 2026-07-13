"""Qdrant client wrapper."""

from __future__ import annotations

from uuid import NAMESPACE_DNS, uuid5

from config import settings
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
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


class EmbeddingModelMismatchError(Exception):
    """Raised when a query is embedded with a different model than the one
    the collection was built with. Same-dimension/different-model is the
    silent killer: cosine similarities collapse to ~0 and nothing relevant
    is ever retrieved. Fail loudly with both model names instead."""

    def __init__(self, collection: str, index_model: str, query_model: str):
        self.collection = collection
        self.index_model = index_model
        self.query_model = query_model
        super().__init__(
            f"Collection '{collection}' was indexed with embedding model "
            f"'{index_model}', but the query uses '{query_model}'. "
            f"Re-ingest the collection with the current model."
        )


# Payload key under which the embedding model name is stamped on every point.
_EMBED_MODEL_KEY = "_embed_model"


def get_client() -> QdrantClient:
    """Return the shared Qdrant client, creating it on first call."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = QdrantClient(
            host=settings.QDRANT_HOST,
            port=settings.QDRANT_PORT,
            timeout=settings.QDRANT_TIMEOUT,
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
    embed_model: str | None = None,
) -> int:
    """Upsert embedding points into a Qdrant collection.

    Auto-creates the collection if it does not exist.
    Returns the number of upserted points.

    ``embed_model`` (when provided) is stamped onto every point so a later
    query with a different model can be detected.
    """
    client = get_client()
    if not embeddings:
        return 0

    dimension = len(embeddings[0]["vector"])
    try:
        _ensure_collection(client, collection, dimension)
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant unreachable: {exc}") from exc

    model_stamp = {_EMBED_MODEL_KEY: embed_model} if embed_model else {}
    points = [
        PointStruct(
            id=_to_uuid(emb["chunk_id"]),
            vector=emb["vector"],
            payload={
                **emb.get("metadata", {}),
                **model_stamp,
                "chunk_id": emb["chunk_id"],
            },
        )
        for emb in embeddings
    ]

    # Upsert in bounded batches — a single huge upsert (hundreds of 4096-dim
    # vectors from a large PDF) overruns the client timeout. ``wait=True`` keeps
    # each batch durable before moving on.
    batch_size = max(1, settings.UPSERT_BATCH_SIZE)
    try:
        for start in range(0, len(points), batch_size):
            client.upsert(
                collection_name=collection,
                points=points[start : start + batch_size],
                wait=True,
            )
    except Exception as exc:
        msg = str(exc)
        if "dimension error" in msg.lower():
            raise QdrantUnavailableError(
                f"Embedding-Dimension passt nicht zur Collection '{collection}' "
                f"(neuer Vektor hat {dimension} Dimensionen). Die Collection wurde "
                f"mit einem anderen Embedding-Modell angelegt. Entweder dasselbe "
                f"Modell verwenden oder die Collection löschen und neu ingesten. "
                f"Original-Fehler: {msg}"
            ) from exc
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
    embed_model: str | None = None,
) -> list[dict]:
    """Search for similar vectors in a collection.

    If ``embed_model`` is given and the collection was stamped with a
    different model at ingest, raises ``EmbeddingModelMismatchError`` rather
    than returning silently meaningless results.
    """
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

    results = [
        {
            "chunk_id": r.payload.get("chunk_id", str(r.id)),
            "score": r.score,
            "text": r.payload.get("text", ""),
            "metadata": r.payload,
        }
        for r in response.points
    ]

    # Consistency guard: compare the query model against the model stamped on
    # the index. Only enforced when both are known — legacy points without a
    # stamp can't be verified, so they pass (re-ingest stamps them).
    if embed_model and results:
        index_model = next(
            (
                r["metadata"].get(_EMBED_MODEL_KEY)
                for r in results
                if r["metadata"].get(_EMBED_MODEL_KEY)
            ),
            None,
        )
        if index_model and index_model != embed_model:
            raise EmbeddingModelMismatchError(collection, index_model, embed_model)

    return results


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
        embed_model = None
        try:
            sample, _ = client.scroll(
                collection_name=c.name, limit=1, with_payload=True
            )
            if sample:
                embed_model = (sample[0].payload or {}).get(_EMBED_MODEL_KEY)
        except Exception:  # noqa: BLE001 — diagnostics only, never fail listing
            pass
        result.append({
            "name": c.name,
            "count": info.points_count or 0,
            "dimension": dimension,
            "embed_model": embed_model,
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


def delete_points_by_filter(collection: str, filters: dict) -> int:
    """Delete every point in ``collection`` matching ``filters``.

    Returns the number of points that matched the filter (counted before
    delete; Qdrant's delete response doesn't carry that number).
    Silently returns 0 when the collection doesn't exist — the caller is
    cleaning up after a doc and that situation is fine.
    """
    if not filters:
        return 0
    client = get_client()
    try:
        existing = {c.name for c in client.get_collections().collections}
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant unreachable: {exc}") from exc
    if collection not in existing:
        return 0
    qfilter = _build_filter(filters)
    if qfilter is None:
        return 0
    try:
        count_resp = client.count(
            collection_name=collection,
            count_filter=qfilter,
            exact=True,
        )
        matched = int(getattr(count_resp, "count", 0) or 0)
        if matched == 0:
            return 0
        client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=qfilter),
        )
    except Exception as exc:
        raise QdrantUnavailableError(f"Qdrant delete failed: {exc}") from exc
    return matched


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

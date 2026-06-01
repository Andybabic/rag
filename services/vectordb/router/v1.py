from __future__ import annotations

from core.qdrant import (
    EmbeddingModelMismatchError,
    QdrantUnavailableError,
    cross_search,
    delete_collection,
    delete_points_by_filter,
    list_collections,
    search_vectors,
    upsert_vectors,
)
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from models import (
    CrossSearchRequest,
    DeletePointsRequest,
    SearchRequest,
    UpsertRequest,
)

router = APIRouter(prefix="/v1", tags=["v1"])


def _qdrant_error_response(exc: QdrantUnavailableError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "qdrant_unavailable",
            "detail": str(exc),
            "request_id": request_id,
            "service": "vectordb-service",
        },
    )


@router.post("/upsert")
async def upsert(body: UpsertRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        count = upsert_vectors(
            collection=body.collection,
            embeddings=[e.model_dump() for e in body.embeddings],
            embed_model=body.embed_model,
        )
    except QdrantUnavailableError as exc:
        return _qdrant_error_response(exc, request_id)

    return {
        "status": "ok",
        "upserted": count,
        "collection": body.collection,
        "request_id": request_id,
    }


@router.post("/search")
async def search(body: SearchRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        results = search_vectors(
            collection=body.collection,
            vector=body.vector,
            top_k=body.top_k,
            filters=body.filters,
            embed_model=body.embed_model,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "error": "collection_not_found",
                "detail": str(exc),
                "request_id": request_id,
                "service": "vectordb-service",
            },
        )
    except EmbeddingModelMismatchError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "embedding_model_mismatch",
                "detail": str(exc),
                "collection": exc.collection,
                "index_model": exc.index_model,
                "query_model": exc.query_model,
                "request_id": request_id,
                "service": "vectordb-service",
            },
        )
    except QdrantUnavailableError as exc:
        return _qdrant_error_response(exc, request_id)

    return {
        "results": results,
        "total": len(results),
        "collection": body.collection,
        "request_id": request_id,
    }


@router.post("/search/cross")
async def search_cross(body: CrossSearchRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        results = cross_search(
            primary_collection=body.primary_collection,
            linked_collections=body.linked_collections,
            vector=body.vector,
            link_key=body.link_key,
            top_k=body.top_k,
            filters=body.filters,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "error": "collection_not_found",
                "detail": str(exc),
                "request_id": request_id,
                "service": "vectordb-service",
            },
        )
    except QdrantUnavailableError as exc:
        return _qdrant_error_response(exc, request_id)

    return {
        "results": results,
        "total": len(results),
        "request_id": request_id,
    }


@router.get("/collections")
async def collections(request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        result = list_collections()
    except QdrantUnavailableError as exc:
        return _qdrant_error_response(exc, request_id)

    return {"collections": result, "request_id": request_id}


@router.post("/delete")
async def delete_points(body: DeletePointsRequest, request: Request):
    """Delete all points in ``collection`` matching ``filters`` (e.g.
    ``{"file_hash": "abc..."}``). Idempotent — returns deleted=0 when the
    collection or the matching points don't exist."""
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        deleted = delete_points_by_filter(body.collection, body.filters)
    except QdrantUnavailableError as exc:
        return _qdrant_error_response(exc, request_id)
    return {
        "status": "ok",
        "deleted": deleted,
        "collection": body.collection,
        "request_id": request_id,
    }


@router.delete("/collection/{name}")
async def collection_delete(name: str, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        delete_collection(name)
    except ValueError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "error": "collection_not_found",
                "detail": str(exc),
                "request_id": request_id,
                "service": "vectordb-service",
            },
        )
    except QdrantUnavailableError as exc:
        return _qdrant_error_response(exc, request_id)

    return {
        "status": "ok",
        "deleted": name,
        "request_id": request_id,
    }

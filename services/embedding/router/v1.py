from __future__ import annotations

from core.ollama import OllamaUnavailableError, embed_batch, embed_text, list_models
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from models import BatchEmbedRequest, EmbedRequest

router = APIRouter(prefix="/v1", tags=["v1"])


@router.post("/embed")
async def embed(body: EmbedRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        vector = await embed_text(body.content)
    except OllamaUnavailableError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "ollama_unavailable",
                "detail": str(exc),
                "request_id": request_id,
                "service": "embedding-service",
            },
        )

    from config import settings

    return {
        "vector": vector,
        "metadata": body.metadata,
        "model": settings.EMBEDDING_MODEL,
        "dimension": len(vector),
        "request_id": request_id,
    }


@router.post("/embed/batch")
async def embed_batch_endpoint(body: BatchEmbedRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")

    from config import settings

    batch_size = body.batch_size or settings.EMBED_BATCH_SIZE
    texts = [c.content for c in body.chunks]

    embeddings = []
    errors: list[str] = []
    failed = 0

    try:
        vectors = await embed_batch(texts, batch_size=batch_size)
    except OllamaUnavailableError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "ollama_unavailable",
                "detail": str(exc),
                "request_id": request_id,
                "service": "embedding-service",
            },
        )

    for i, chunk in enumerate(body.chunks):
        chunk_id = chunk.metadata.get("chunk_id", f"chunk_{i}")
        if i < len(vectors):
            embeddings.append({
                "chunk_id": chunk_id,
                "vector": vectors[i],
                "model": settings.EMBEDDING_MODEL,
                "dimension": len(vectors[i]),
            })
        else:
            failed += 1
            errors.append(f"Missing vector for chunk {chunk_id}")

    return {
        "embeddings": embeddings,
        "total": len(body.chunks),
        "failed": failed,
        "errors": errors,
        "request_id": request_id,
    }


@router.get("/models")
async def models_endpoint(request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        model_list = await list_models()
    except OllamaUnavailableError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "ollama_unavailable",
                "detail": str(exc),
                "request_id": request_id,
                "service": "embedding-service",
            },
        )

    return {"models": model_list, "request_id": request_id}

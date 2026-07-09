from __future__ import annotations

from core.ollama import (
    OllamaUnavailableError,
    embed_batch,
    embed_text,
    list_models,
    resolve_embedding,
)
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from models import BatchEmbedRequest, EmbedRequest

router = APIRouter(prefix="/v1", tags=["v1"])


def _unavailable(exc: Exception, request_id: str) -> JSONResponse:
    # The backend may be Ollama or an OpenAI-compatible endpoint; keep a
    # provider-agnostic error code but let the detail carry the real cause.
    return JSONResponse(
        status_code=503,
        content={
            "error": "embedding_unavailable",
            "detail": str(exc),
            "request_id": request_id,
            "service": "embedding-service",
        },
    )


@router.post("/embed")
async def embed(body: EmbedRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    cfg, model = await resolve_embedding(body.use_case, body.model)
    try:
        vector = await embed_text(body.content, model=model, config=cfg)
    except OllamaUnavailableError as exc:
        return _unavailable(exc, request_id)

    return {
        "vector": vector,
        "metadata": body.metadata,
        "model": model,
        "dimension": len(vector),
        "request_id": request_id,
    }


@router.post("/embed/batch")
async def embed_batch_endpoint(body: BatchEmbedRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")

    from config import settings

    batch_size = body.batch_size or settings.EMBED_BATCH_SIZE
    texts = [c.content for c in body.chunks]

    cfg, model = await resolve_embedding(body.use_case, body.model)

    embeddings = []
    errors: list[str] = []
    failed = 0

    try:
        vectors = await embed_batch(texts, model=model, batch_size=batch_size, config=cfg)
    except OllamaUnavailableError as exc:
        return _unavailable(exc, request_id)

    for i, chunk in enumerate(body.chunks):
        chunk_id = chunk.metadata.get("chunk_id", f"chunk_{i}")
        if i < len(vectors):
            embeddings.append({
                "chunk_id": chunk_id,
                "vector": vectors[i],
                "model": model,
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
        return _unavailable(exc, request_id)

    return {"models": model_list, "request_id": request_id}

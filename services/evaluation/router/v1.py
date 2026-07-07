from __future__ import annotations

import json
import logging

import asyncio

import asyncpg
from core.citations import map_citations
from core.database import get_pool
from core.evaluator import evaluate_chunks
from core.manager import run_manager
from core.reranker import rerank_chunks
from core.use_cases import (
    get_agent_actions,
    get_default_collection,
    get_system_prompt,
    get_use_case_prefixes,
)
import httpx
from config import settings
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from models import (
    AgentQueryRequest,
    CitationsRequest,
    EvaluateRequest,
    FeedbackRequest,
    RerankRequest,
)
from shared.usecase_config import list_use_cases

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/use-cases")
async def get_use_cases():
    """List enabled use cases in the frontend's shape (for the use-case picker).

    Mirrors the ``UseCaseDef`` interface used by the SvelteKit frontend.
    """
    cases = await list_use_cases(only_enabled=True)
    return {
        "use_cases": [
            {
                "apiId": uc.id,
                "slug": uc.slug,
                "label": uc.label,
                "desc": uc.description,
                "color": uc.color,
                "accent": uc.accent,
                "roles": uc.roles,
            }
            for uc in cases
        ]
    }


@router.post("/rerank")
async def rerank(body: RerankRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    chunks = [c.model_dump() for c in body.chunks]
    reranked = rerank_chunks(body.query, chunks, top_n=len(chunks))
    above = [r for r in reranked if r["rerank_score"] >= body.config.threshold]
    below = [r for r in reranked if r["rerank_score"] < body.config.threshold]

    return {
        "reranked": above,
        "threshold_passed": len(above) > 0,
        "below_threshold": below,
        "total": len(chunks),
        "request_id": request_id,
    }


@router.post("/evaluate")
async def evaluate(body: EvaluateRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    chunks = [c.model_dump() for c in body.chunks]
    result = evaluate_chunks(
        query=body.query,
        chunks=chunks,
        min_chunks=body.min_chunks,
        min_score=body.min_score,
    )
    return {**result, "request_id": request_id}


@router.post("/citations")
async def citations(body: CitationsRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    chunks = [c.model_dump() for c in body.chunks]
    result = map_citations(body.answer, chunks)
    return {"citations": result, "request_id": request_id}


@router.post("/agent/query")
async def agent_query(body: AgentQueryRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")

    try:
        system_prompt = await get_system_prompt(body.use_case, body.role)
        available_actions = get_agent_actions(body.use_case)
        default_collection = get_default_collection(body.use_case)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unknown_use_case",
                "detail": str(exc),
                "request_id": request_id,
                "service": "evaluation-service",
            },
        )

    collection = body.config.collection or default_collection

    history = [{"role": m.role, "content": m.content} for m in body.history]

    result = await run_manager(
        query=body.query,
        use_case=body.use_case,
        session_id=body.session_id,
        use_case_prompt=system_prompt,
        available_actions=available_actions,
        collection=collection,
        filters=body.config.filters,
        history=history,
        images=body.images,
    )

    # Persist query to database (non-blocking)
    try:
        pool = await get_pool()
        # Use the request_id as the query's primary key so user feedback
        # (which only knows the request_id) can be linked back to this row.
        await pool.execute(
            """INSERT INTO queries (id, use_case, session_id, role, query_text, answer_text,
                                    agent_steps, citations, images, sufficient)
               VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10)
               ON CONFLICT (id) DO NOTHING""",
            request_id,
            body.use_case,
            body.session_id,
            body.role,
            body.query,
            result.get("answer", ""),
            json.dumps(result.get("agent_steps", [])),
            json.dumps(result.get("citations", [])),
            json.dumps(body.images),
            result.get("sufficient", False),
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(f"Failed to log query: {exc}")

    return {**result, "request_id": request_id}


@router.post("/agent/query/stream")
async def agent_query_stream(body: AgentQueryRequest, request: Request):
    """Server-Sent-Events stream of agent progress.

    Each event is one line of NDJSON. Event types: started, thinking, action,
    step, final, error. The frontend renders them live so the user sees what
    the agent is doing instead of staring at a spinner.
    """
    request_id = getattr(request.state, "request_id", "unknown")

    try:
        system_prompt = await get_system_prompt(body.use_case, body.role)
        available_actions = get_agent_actions(body.use_case)
        default_collection = get_default_collection(body.use_case)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "unknown_use_case", "detail": str(exc),
                     "request_id": request_id, "service": "evaluation-service"},
        )

    collection = body.config.collection or default_collection
    history = [{"role": m.role, "content": m.content} for m in body.history]
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def on_event(event: dict) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            result = await run_manager(
                query=body.query,
                use_case=body.use_case,
                session_id=body.session_id,
                use_case_prompt=system_prompt,
                available_actions=available_actions,
                collection=collection,
                filters=body.config.filters,
                history=history,
                images=body.images,
                on_event=on_event,
            )
            # Persist query (best-effort, same as non-streaming endpoint)
            try:
                pool = await get_pool()
                # request_id as PK — links this row to later user feedback.
                await pool.execute(
                    """INSERT INTO queries (id, use_case, session_id, role, query_text,
                                            answer_text, agent_steps, citations, images, sufficient)
                       VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10)
                       ON CONFLICT (id) DO NOTHING""",
                    request_id,
                    body.use_case, body.session_id, body.role, body.query,
                    result.get("answer", ""),
                    json.dumps(result.get("agent_steps", [])),
                    json.dumps(result.get("citations", [])),
                    json.dumps(body.images),
                    result.get("sufficient", False),
                )
            except Exception as exc:
                logging.getLogger(__name__).warning(f"Failed to log query: {exc}")
        except Exception as exc:
            await queue.put({"type": "error", "detail": str(exc)})
        finally:
            await queue.put(None)  # sentinel: stream done

    async def event_stream():
        task = asyncio.create_task(runner())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                # NDJSON: one JSON object per line, easy to parse incrementally
                yield json.dumps({**event, "request_id": request_id}) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/use-case/{use_case_id}/collections")
async def use_case_collections(use_case_id: str, request: Request):
    """List all VectorDB collections that belong to a specific use case."""
    request_id = getattr(request.state, "request_id", "unknown")

    try:
        prefixes = get_use_case_prefixes(use_case_id)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unknown_use_case",
                "detail": str(exc),
                "request_id": request_id,
                "service": "evaluation-service",
            },
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.VECTORDB_SERVICE_URL}/v1/collections")
            resp.raise_for_status()
            all_collections = resp.json().get("collections", [])
    except httpx.HTTPError:
        all_collections = []

    available = [
        c for c in all_collections
        if any(c["name"].startswith(p) for p in prefixes)
    ]

    return {
        "use_case": use_case_id,
        "default_collection": get_default_collection(use_case_id),
        "collections": available,
        "request_id": request_id,
    }


@router.post("/ingest-log")
async def log_ingestion(body: dict, request: Request):
    """Log a completed ingestion to the database."""
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        pool = await get_pool()
        await pool.execute(
            """INSERT INTO ingestion_log
                   (id, file_name, file_hash, stored_path, collection, use_case, chunk_count, status)
               VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, 'ok')
               ON CONFLICT (file_hash) DO UPDATE SET
                   chunk_count = EXCLUDED.chunk_count,
                   stored_path = EXCLUDED.stored_path,
                   collection = EXCLUDED.collection""",
            body.get("file_name", ""),
            body.get("file_hash", ""),
            body.get("stored_path", ""),
            body.get("collection", ""),
            body.get("use_case", ""),
            body.get("chunk_count", 0),
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(f"Failed to log ingestion: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": "db_error", "detail": str(exc), "request_id": request_id},
        )
    return {"status": "ok", "request_id": request_id}


@router.post("/log")
async def log_feedback(body: FeedbackRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    if body.feedback not in ("positive", "negative"):
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_feedback",
                "detail": "feedback must be 'positive' or 'negative'",
                "request_id": request_id,
                "service": "evaluation-service",
            },
        )
    comment = (body.comment or "").strip() or None

    # The frontend sends the request_id as query_id. We stored it as queries.id,
    # so it usually links directly. If the query row is missing (best-effort
    # logging can fail) or the id isn't a UUID, keep the feedback anyway with a
    # NULL reference rather than losing it.
    try:
        pool = await get_pool()
        try:
            await pool.execute(
                """INSERT INTO feedback (id, query_id, rating, comment)
                   VALUES (gen_random_uuid(), $1::uuid, $2, $3)""",
                body.query_id,
                body.feedback,
                comment,
            )
        except (asyncpg.ForeignKeyViolationError, asyncpg.DataError):
            await pool.execute(
                """INSERT INTO feedback (id, query_id, rating, comment)
                   VALUES (gen_random_uuid(), NULL, $1, $2)""",
                body.feedback,
                comment,
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(f"Failed to persist feedback: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "db_error",
                "detail": str(exc),
                "request_id": request_id,
                "service": "evaluation-service",
            },
        )

    return {
        "status": "ok",
        "query_id": body.query_id,
        "feedback": body.feedback,
        "request_id": request_id,
    }

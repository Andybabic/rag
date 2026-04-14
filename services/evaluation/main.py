import os

from contextlib import asynccontextmanager

from core.database import close_pool, get_pool
from core.reranker import preload_model as preload_reranker
from fastapi import FastAPI, Request
from models import HealthResponse
from router.admin import router as admin_router
from router.v1 import router as v1_router
from shared.errors import handle_error
from shared.logging import setup_logging
from shared.tracing import RequestIDMiddleware

SERVICE_NAME = "evaluation-service"
VERSION = "1.0.0"

setup_logging(SERVICE_NAME, os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    # Start the cross-encoder download in the background so the first query
    # doesn't block for minutes on a cold HF cache. Until it's ready, the
    # reranker falls back to hybrid-fusion ordering (still good quality).
    preload_reranker()
    yield
    await close_pool()


app = FastAPI(
    title="RAG Platform – Evaluation Service",
    description="Reranking, evaluation, and citation mapping",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestIDMiddleware)
app.include_router(v1_router)
app.include_router(admin_router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return handle_error(exc, request, service=SERVICE_NAME)


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", service=SERVICE_NAME, version=VERSION)

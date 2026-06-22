import os

from contextlib import asynccontextmanager

from config import settings
from core.database import close_pool, get_pool
from core.reranker import preload_model as preload_reranker
from core.seed import seed_use_cases
from core.use_cases import refresh_use_cases
from fastapi import FastAPI, Request
from models import HealthResponse
from router.admin import router as admin_router
from router.auth import router as auth_router
from router.v1 import router as v1_router
from shared.auth import ensure_bootstrap_admin
from shared.errors import handle_error
from shared.logging import setup_logging
from shared.tracing import RequestIDMiddleware
from shared.usecase_config import set_pool as set_resolver_pool

SERVICE_NAME = "evaluation-service"
VERSION = "1.0.0"

setup_logging(SERVICE_NAME, os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    set_resolver_pool(pool)
    # Seed the first admin from env if no users exist yet (idempotent).
    await ensure_bootstrap_admin(settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD)
    # Seed missing use cases from config/use_cases.json, then load the registry
    # (meta + collection prefixes) from the DB into memory.
    await seed_use_cases()
    await refresh_use_cases()
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
app.include_router(auth_router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return handle_error(exc, request, service=SERVICE_NAME)


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", service=SERVICE_NAME, version=VERSION)


@app.get("/health/reranker")
async def health_reranker():
    """Reranker diagnostic – check whether the cross-encoder is actually
    active. ``degraded: true`` means retrieval runs on hybrid-fusion only."""
    from core.reranker import reranker_status

    return reranker_status()

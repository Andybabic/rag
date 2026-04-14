import os

from fastapi import FastAPI, Request
from models import HealthResponse
from router.v1 import router as v1_router
from shared.errors import handle_error
from shared.logging import setup_logging
from shared.tracing import RequestIDMiddleware

SERVICE_NAME = "vectordb-service"
VERSION = "1.0.0"

setup_logging(SERVICE_NAME, os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(
    title="RAG Platform – Vector DB Service",
    description="Qdrant wrapper for vector storage and similarity search",
    version=VERSION,
)
app.add_middleware(RequestIDMiddleware)
app.include_router(v1_router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return handle_error(exc, request, service=SERVICE_NAME)


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", service=SERVICE_NAME, version=VERSION)

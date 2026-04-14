"""Centralized error handling for all RAG platform services."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from shared.models import APIError

logger = logging.getLogger(__name__)

_STATUS_MAP: dict[type, int] = {
    ValueError: 400,
    FileNotFoundError: 404,
    PermissionError: 403,
}


def handle_error(exc: Exception, request: Request, service: str) -> JSONResponse:
    """Convert an exception into a standardised APIError JSON response."""
    status_code = _STATUS_MAP.get(type(exc), 500)
    request_id = getattr(request.state, "request_id", "unknown")

    logger.error(
        "Unhandled %s: %s",
        type(exc).__name__,
        exc,
        exc_info=exc,
        extra={
            "extra_data": {
                "error": type(exc).__name__,
                "status_code": status_code,
                "path": str(request.url.path),
            }
        },
    )

    body = APIError(
        error=type(exc).__name__,
        detail=str(exc),
        request_id=request_id,
        service=service,
    )

    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
    )

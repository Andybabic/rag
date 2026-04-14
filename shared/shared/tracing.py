"""Request-ID middleware for tracing requests across services."""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from shared.logging import request_id_ctx

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Read or generate X-Request-ID, store it on request.state, and echo it back."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        request_id_ctx.set(request_id)

        method = request.method
        path = request.url.path

        logger.info(
            "Request started: %s %s",
            method,
            path,
            extra={"extra_data": {"method": method, "path": path}},
        )

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        logger.info(
            "Request finished: %s %s %s",
            method,
            path,
            response.status_code,
            extra={
                "extra_data": {
                    "method": method,
                    "path": path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                }
            },
        )

        response.headers["X-Request-ID"] = request_id
        return response

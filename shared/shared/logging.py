"""Structured JSON logging for all RAG platform services."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Context variable for request_id – set by RequestIDMiddleware.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")

# Context variable for service name – set once at startup.
service_name_ctx: ContextVar[str] = ContextVar("service_name", default="unknown")


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "service": service_name_ctx.get(),
            "request_id": request_id_ctx.get() or None,
            "message": record.getMessage(),
            "extra": {},
        }

        # Merge any user-supplied extra fields.
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            entry["extra"] = record.extra_data

        # Include exception info when present.
        if record.exc_info and record.exc_info[0] is not None:
            entry["extra"]["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(service: str, level: str = "INFO") -> None:
    """Configure structured JSON logging for a service.

    Call once at application startup (before any log is emitted).
    """
    service_name_ctx.set(service)

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Silence noisy third-party loggers.
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that participates in structured logging."""
    return logging.getLogger(name)


def log_extra(logger: logging.Logger, level: int, msg: str, **kwargs: Any) -> None:
    """Log a message with structured extra data."""
    logger.log(level, msg, extra={"extra_data": kwargs})

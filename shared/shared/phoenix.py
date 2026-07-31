"""Phoenix / OpenInference tracing setup.

Configures an OpenTelemetry TracerProvider that exports to a local
Arize Phoenix instance via OTLP/HTTP.  Call ``setup_phoenix()``
once at application startup; the returned TracerProvider is also
available as a module-level singleton for custom spans.

Environment variables
---------------------
PHOENIX_URL : str
    Base URL of the Phoenix collector (default ``http://phoenix:6006``).
PHOENIX_PROJECT : str
    Project name inside Phoenix (default ``default``).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# ── Lazy singletons ────────────────────────────────────────────────
_tracer_provider = None


def get_tracer_provider():
    """Return the global TracerProvider (or None if not initialised)."""
    return _tracer_provider


def get_tracer(name: str = "rag-platform"):
    """Shorthand: return a Tracer from the global provider."""
    if _tracer_provider is None:
        raise RuntimeError("Phoenix tracer provider not initialised – call setup_phoenix() first")
    return _tracer_provider.get_tracer(name)


def setup_phoenix(app: FastAPI, service_name: str = "rag-platform") -> None:
    """Instrument FastAPI + wire OTLP exporter to Phoenix.

    This is safe to call multiple times; subsequent calls are no-ops.
    """
    global _tracer_provider

    if _tracer_provider is not None:
        return  # already initialised

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        phoenix_url = os.getenv("PHOENIX_URL", "http://phoenix:6006")
        project = os.getenv("PHOENIX_PROJECT", "default")
        endpoint = f"{phoenix_url}/v1/traces"

        resource = Resource.create({
            "service.name": service_name,
            "openinference.project.name": project,
        })

        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _tracer_provider = provider

        # Auto-instrument FastAPI + httpx for HTTP spans
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
            logger.info("FastAPI instrumented for OpenTelemetry")
        except Exception as exc:
            logger.warning("FastAPI auto-instrumentation skipped: %s", exc)

        logger.info(
            "Phoenix tracing initialised → %s (project=%s)",
            endpoint,
            project,
        )
    except ImportError as exc:
        logger.warning("OpenTelemetry packages not available – tracing disabled: %s", exc)
    except Exception as exc:
        logger.error("Failed to initialise Phoenix tracing: %s", exc)


async def log_evaluation_to_phoenix(
    evaluation_name: str,
    score: float | None = None,
    label: str | None = None,
    explanation: str | None = None,
    query_id: str | None = None,
    metadata: dict | None = None,
    span_id: str | None = None,
) -> bool:
    """Send an evaluation annotation to Phoenix via its span-annotations REST API.

    Attaches to the current OpenTelemetry span if one is active so the
    annotation appears in Phoenix's trace view. Falls back silently if
    Phoenix is unreachable.

    Returns True if the annotation was accepted, False otherwise.
    """
    try:
        import httpx
        from opentelemetry import trace
    except ImportError:
        logger.debug("OpenTelemetry not available – skipping Phoenix annotation for %s", evaluation_name)
        return False

    phoenix_url = os.getenv("PHOENIX_URL", "http://phoenix:6006")

    if span_id is None:
        try:
            current_span = trace.get_current_span()
            if current_span is not None:
                ctx = current_span.get_span_context()
                if ctx.is_valid:
                    span_id = format(ctx.span_id, "016x")
        except Exception:
            pass

    if not span_id:
        logger.debug("No active OTEL span – skipping Phoenix annotation for %s", evaluation_name)
        return False

    endpoint = f"{phoenix_url}/v1/span_annotations"

    result: dict = {}
    if score is not None:
        result["score"] = score
    if label is not None:
        result["label"] = label
    if explanation is not None:
        result["explanation"] = explanation

    annotation_meta: dict = dict(metadata or {})
    if query_id:
        annotation_meta["query_id"] = query_id

    payload = {
        "name": evaluation_name,
        "annotator_kind": "CODE",
        "result": result,
        "span_id": span_id,
    }
    if annotation_meta:
        payload["metadata"] = annotation_meta

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                endpoint,
                json={"data": [payload]},
                headers={"Content-Type": "application/json"},
            )
            if resp.is_success:
                logger.info("Phoenix annotation logged: %s=%s (span=%s)", evaluation_name, label or score, span_id)
                return True
            else:
                logger.warning(
                    "Phoenix annotation rejected (%d): %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
    except Exception as exc:
        logger.debug("Phoenix annotation skipped (unreachable): %s", exc)
        return False


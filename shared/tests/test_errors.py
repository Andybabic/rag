"""Tests for centralized error handling."""

import httpx
import pytest
from shared.errors import handle_error
from shared.tracing import RequestIDMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route


def _build_app():
    async def raise_value_error(request: Request):
        raise ValueError("bad input")

    async def raise_file_not_found(request: Request):
        raise FileNotFoundError("doc.pdf not found")

    async def raise_generic(request: Request):
        raise RuntimeError("unexpected")

    async def ok(request: Request):
        return PlainTextResponse("ok")

    async def error_handler(request: Request, exc: Exception):
        return handle_error(exc, request, service="test-service")

    app = Starlette(
        routes=[
            Route("/value-error", raise_value_error),
            Route("/not-found", raise_file_not_found),
            Route("/generic", raise_generic),
            Route("/ok", ok),
        ],
        exception_handlers={
            ValueError: error_handler,
            FileNotFoundError: error_handler,
            RuntimeError: error_handler,
        },
    )
    app.add_middleware(RequestIDMiddleware)

    return app


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=_build_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_value_error_returns_400(client):
    resp = await client.get("/value-error")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "ValueError"
    assert body["service"] == "test-service"


@pytest.mark.anyio
async def test_file_not_found_returns_404(client):
    resp = await client.get("/not-found")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_generic_error_returns_500(client):
    resp = await client.get("/generic")
    assert resp.status_code == 500
    assert resp.json()["error"] == "RuntimeError"

"""Tests for X-Request-ID middleware."""

import httpx
import pytest
from shared.tracing import RequestIDMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def _echo_request_id(request: Request):
    return JSONResponse({"request_id": request.state.request_id})


def _build_app():
    app = Starlette(routes=[Route("/", _echo_request_id)])
    app.add_middleware(RequestIDMiddleware)
    return app


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=_build_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_generates_request_id_when_missing(client):
    resp = await client.get("/")
    assert "x-request-id" in resp.headers
    body = resp.json()
    assert body["request_id"] == resp.headers["x-request-id"]


@pytest.mark.anyio
async def test_preserves_incoming_request_id(client):
    resp = await client.get("/", headers={"X-Request-ID": "my-trace-42"})
    assert resp.headers["x-request-id"] == "my-trace-42"
    assert resp.json()["request_id"] == "my-trace-42"

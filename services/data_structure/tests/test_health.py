import httpx
import pytest
from main import app


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "data-structure-service"
    assert body["version"] == "1.0.0"


@pytest.mark.anyio
async def test_swagger_ui(client):
    resp = await client.get("/docs")
    assert resp.status_code == 200

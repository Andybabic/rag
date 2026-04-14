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
    assert body["service"] == "cleaning-service"
    assert body["version"] == "1.0.0"


@pytest.mark.anyio
async def test_formats(client):
    resp = await client.get("/v1/formats")
    assert resp.status_code == 200
    formats = resp.json()
    assert ".pdf" in formats
    assert ".docx" in formats
    assert ".csv" in formats
    assert ".txt" in formats
    assert ".md" in formats


@pytest.mark.anyio
async def test_swagger_ui(client):
    resp = await client.get("/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower()

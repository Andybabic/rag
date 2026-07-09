"""Tests for embedding endpoints with mocked Ollama."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from main import app

FAKE_VECTOR = [0.1] * 1024


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _embed_request(content: str = "Test text", **overrides):
    body = {
        "type": "text",
        "content": content,
        "metadata": {
            "chunk_id": "test-uuid",
            "file_name": "handbuch.pdf",
            "collection": "neumann_machines",
        },
    }
    body.update(overrides)
    return body


def _batch_request(n: int = 3, batch_size: int | None = None):
    body: dict = {
        "chunks": [
            {
                "type": "text",
                "content": f"Chunk {i}",
                "metadata": {"chunk_id": f"id-{i}"},
            }
            for i in range(n)
        ],
    }
    if batch_size is not None:
        body["batch_size"] = batch_size
    return body


# ── POST /v1/embed ─────────────────────────────────────────


@pytest.mark.anyio
@patch("router.v1.embed_text", new_callable=AsyncMock, return_value=FAKE_VECTOR)
async def test_embed_single(mock_embed, client):
    resp = await client.post("/v1/embed", json=_embed_request())
    assert resp.status_code == 200
    body = resp.json()
    assert body["vector"] == FAKE_VECTOR
    assert body["dimension"] == 1024
    assert body["model"] == "qwen3-embedding:0.6b"
    assert body["metadata"]["chunk_id"] == "test-uuid"
    assert "request_id" in body


@pytest.mark.anyio
@patch("router.v1.embed_text", new_callable=AsyncMock, return_value=FAKE_VECTOR)
async def test_embed_preserves_metadata(mock_embed, client):
    resp = await client.post("/v1/embed", json=_embed_request())
    body = resp.json()
    assert body["metadata"]["file_name"] == "handbuch.pdf"
    assert body["metadata"]["collection"] == "neumann_machines"


@pytest.mark.anyio
@patch("router.v1.embed_text", new_callable=AsyncMock)
async def test_embed_ollama_unavailable(mock_embed, client):
    from core.ollama import OllamaUnavailableError

    mock_embed.side_effect = OllamaUnavailableError("Connection refused")
    resp = await client.post("/v1/embed", json=_embed_request())
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "embedding_unavailable"


# ── POST /v1/embed/batch ───────────────────────────────────


@pytest.mark.anyio
@patch("router.v1.embed_batch", new_callable=AsyncMock)
async def test_batch_embed(mock_batch, client):
    mock_batch.return_value = [FAKE_VECTOR] * 3
    resp = await client.post("/v1/embed/batch", json=_batch_request(n=3))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["failed"] == 0
    assert len(body["embeddings"]) == 3
    assert body["embeddings"][0]["dimension"] == 1024
    assert body["embeddings"][0]["chunk_id"] == "id-0"


@pytest.mark.anyio
@patch("router.v1.embed_batch", new_callable=AsyncMock)
async def test_batch_passes_batch_size(mock_batch, client):
    mock_batch.return_value = [FAKE_VECTOR] * 100
    resp = await client.post("/v1/embed/batch", json=_batch_request(n=100, batch_size=50))
    assert resp.status_code == 200
    # Verify embed_batch was called with batch_size=50
    mock_batch.assert_called_once()
    call_kwargs = mock_batch.call_args
    assert call_kwargs.kwargs["batch_size"] == 50


@pytest.mark.anyio
@patch("router.v1.embed_batch", new_callable=AsyncMock)
async def test_batch_ollama_unavailable(mock_batch, client):
    from core.ollama import OllamaUnavailableError

    mock_batch.side_effect = OllamaUnavailableError("Connection refused")
    resp = await client.post("/v1/embed/batch", json=_batch_request())
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "embedding_unavailable"


# ── GET /v1/models ─────────────────────────────────────────


@pytest.mark.anyio
@patch("router.v1.list_models", new_callable=AsyncMock)
async def test_models_endpoint(mock_list, client):
    mock_list.return_value = [
        {"name": "qwen3-embedding:0.6b", "size": 600_000_000},
        {"name": "nomic-embed-text", "size": 300_000_000},
    ]
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["models"]) == 2
    assert body["models"][0]["name"] == "qwen3-embedding:0.6b"


@pytest.mark.anyio
@patch("router.v1.list_models", new_callable=AsyncMock)
async def test_models_ollama_unavailable(mock_list, client):
    from core.ollama import OllamaUnavailableError

    mock_list.side_effect = OllamaUnavailableError("Connection refused")
    resp = await client.get("/v1/models")
    assert resp.status_code == 503

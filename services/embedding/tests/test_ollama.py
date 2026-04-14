"""Tests for the Ollama client module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from core.ollama import OllamaUnavailableError, embed_batch, embed_text, list_models

FAKE_VECTOR = [0.1] * 1024


@pytest.mark.anyio
@patch("core.ollama.httpx.AsyncClient")
async def test_embed_text_returns_vector(mock_client_cls):
    mock_response = MagicMock()
    mock_response.json.return_value = {"embedding": FAKE_VECTOR}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await embed_text("Test text")
    assert result == FAKE_VECTOR
    assert len(result) == 1024


@pytest.mark.anyio
@patch("core.ollama.httpx.AsyncClient")
async def test_embed_text_connect_error_raises(mock_client_cls):
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    with pytest.raises(OllamaUnavailableError, match="not reachable"):
        await embed_text("Test text")


@pytest.mark.anyio
@patch("core.ollama.httpx.AsyncClient")
async def test_embed_text_http_error_raises(mock_client_cls):
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Server Error",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    with pytest.raises(OllamaUnavailableError, match="returned 500"):
        await embed_text("Test text")


@pytest.mark.anyio
@patch("core.ollama.embed_text", new_callable=AsyncMock, return_value=FAKE_VECTOR)
async def test_embed_batch_splits_into_batches(mock_embed):
    texts = [f"text_{i}" for i in range(100)]
    result = await embed_batch(texts, batch_size=50)
    assert len(result) == 100
    # 100 texts with batch_size=50 → embed_text called 100 times (once per text)
    assert mock_embed.call_count == 100


@pytest.mark.anyio
@patch("core.ollama.embed_text", new_callable=AsyncMock, return_value=FAKE_VECTOR)
async def test_embed_batch_all_vectors_returned(mock_embed):
    texts = ["text_a", "text_b", "text_c"]
    result = await embed_batch(texts, batch_size=2)
    assert len(result) == 3
    assert all(v == FAKE_VECTOR for v in result)


@pytest.mark.anyio
@patch("core.ollama.httpx.AsyncClient")
async def test_list_models_returns_models(mock_client_cls):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "models": [{"name": "qwen3-embedding:0.6b"}]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await list_models()
    assert len(result) == 1
    assert result[0]["name"] == "qwen3-embedding:0.6b"


@pytest.mark.anyio
@patch("core.ollama.httpx.AsyncClient")
async def test_list_models_connect_error_raises(mock_client_cls):
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    with pytest.raises(OllamaUnavailableError, match="not reachable"):
        await list_models()

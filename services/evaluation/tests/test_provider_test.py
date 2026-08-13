"""Connection-test helper: German error copy and provider probes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from core.provider_test import (
    explain_provider_error,
    list_chat_models,
    overlay_config,
    run_connection_tests,
)
from shared.llm.config import LLMConfig
from shared.llm.errors import LLMUnavailableError


def _explain(exc: BaseException, **kwargs) -> dict[str, str]:
    return explain_provider_error(
        exc,
        provider=kwargs.get("provider", "openai"),
        base_url=kwargs.get("base_url", "https://api.openai.com/v1"),
        model=kwargs.get("model", "gpt-4o-mini"),
    )


def test_explain_unreachable():
    out = _explain(
        LLMUnavailableError(
            "Ollama not reachable / timed out at http://ollama:11434: ConnectError"
        ),
        provider="ollama",
        base_url="http://ollama:11434",
    )
    assert out["code"] == "unreachable"
    assert "nicht erreichbar" in out["message"]
    assert "Base-URL" in out["hint"]


def test_explain_timeout():
    out = _explain(TimeoutError("timed out after 45s at https://api.openai.com/v1"))
    assert out["code"] == "timeout"
    assert "Zeitüberschreitung" in out["message"]


def test_explain_auth_from_status():
    out = _explain(
        LLMUnavailableError(
            "OpenAI returned 401: "
            '{"error":{"message":"Incorrect API key provided: sk-secretvalue123",'
            '"type":"invalid_api_key"}}'
        )
    )
    assert out["code"] == "auth_failed"
    assert "Authentifizierung" in out["message"]
    assert "sk-secretvalue123" not in out["detail"]


def test_explain_quota():
    out = _explain(
        LLMUnavailableError(
            "OpenAI returned 429: "
            '{"error":{"code":"insufficient_quota",'
            '"message":"You exceeded your current quota"}}'
        )
    )
    assert out["code"] == "quota"
    assert "Kontingent" in out["message"]


def test_explain_model_not_found():
    out = _explain(
        LLMUnavailableError(
            "OpenAI returned 404: "
            '{"error":{"code":"model_not_found",'
            '"message":"The model `gpt-x` does not exist"}}'
        ),
        model="gpt-x",
    )
    assert out["code"] == "model_not_found"
    assert "gpt-x" in out["message"]


def test_explain_missing_openai_key():
    out = _explain(
        LLMUnavailableError("OPENAI_API_KEY is required when using the openai provider"),
        provider="openai",
    )
    assert out["code"] == "missing_key"
    assert "API-Key" in out["message"]


def test_explain_unknown_provider():
    out = _explain(
        ValueError("Unknown LLM provider: 'foo'. Registered providers: ollama, openai"),
        provider="foo",
        base_url="",
    )
    assert out["code"] == "unknown_provider"


def test_explain_404_mentions_v1_for_openai():
    out = _explain(
        LLMUnavailableError('OpenAI returned 404: {"detail":"Not Found"}'),
        provider="openai",
        base_url="https://nemotron.sparklab.media.ustp.at/",
    )
    assert out["code"] == "not_found"
    assert "/v1" in out["hint"]


def test_explain_ssl():
    out = _explain(
        LLMUnavailableError("SSL: CERTIFICATE_VERIFY_FAILED at https://internal.llm"),
        base_url="https://internal.llm",
    )
    assert out["code"] == "ssl"


def test_overlay_ignores_empty_and_applies_typed_key():
    base = LLMConfig.from_env()
    patched = overlay_config(
        base,
        {
            "chat_provider": "openai",
            "openai_base_url": "https://proxy.example/v1",
            "llm_model": "",
            "openai_api_key": "sk-new",
            "ollama_api_key": "  ",
        },
    )
    assert patched.chat_provider == "openai"
    assert patched.openai_base_url == "https://proxy.example/v1"
    assert patched.llm_model == base.llm_model
    assert patched.openai_api_key == "sk-new"
    assert patched.ollama_api_key == base.ollama_api_key


def _provider(models: list[dict], chat_reply: str = "pong"):
    p = MagicMock()
    p.list_models = AsyncMock(return_value=models)
    p.chat = AsyncMock(return_value=chat_reply)
    return p


@pytest.mark.anyio
async def test_run_connection_success_skips_same_vision():
    cfg = LLMConfig(
        chat_provider="ollama",
        vision_provider="ollama",
        ollama_base_url="http://ollama:11434",
        llm_model="qwen3:8b",
        vision_model="qwen3:8b",
    )
    fake = _provider([{"name": "qwen3:8b"}, {"name": "llava"}])
    with patch("core.provider_test.get_provider", return_value=fake):
        result = await run_connection_tests(cfg)
    assert result["ok"] is True
    chat, vision = result["checks"]
    assert chat["ok"] is True
    assert chat["code"] == "ok"
    assert "Chat-Ping" in chat["message"]
    assert vision["skipped"] is True
    fake.chat.assert_awaited()


@pytest.mark.anyio
async def test_run_connection_auth_failure():
    cfg = LLMConfig(
        chat_provider="openai",
        openai_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
    )
    fake = MagicMock()
    fake.list_models = AsyncMock(
        side_effect=LLMUnavailableError(
            'OpenAI returned 401: {"error":{"message":"Incorrect API key provided"}}'
        )
    )
    with patch("core.provider_test.get_provider", return_value=fake):
        result = await run_connection_tests(cfg)
    assert result["ok"] is False
    assert result["checks"][0]["code"] == "auth_failed"
    assert "Authentifizierung" in result["checks"][0]["message"]
    fake.chat.assert_not_called()


@pytest.mark.anyio
async def test_list_chat_models_maps_connect_error():
    cfg = LLMConfig(chat_provider="ollama", ollama_base_url="http://missing:11434")
    fake = MagicMock()
    fake.list_models = AsyncMock(
        side_effect=LLMUnavailableError(
            f"Ollama not reachable / timed out at {cfg.ollama_base_url}: "
            f"{httpx.ConnectError('fail')}"
        )
    )
    with patch("core.provider_test.get_provider", return_value=fake):
        result = await list_chat_models(cfg)
    assert result["models"] == []
    assert result["error"] == "unreachable"
    assert "nicht erreichbar" in result["message"]

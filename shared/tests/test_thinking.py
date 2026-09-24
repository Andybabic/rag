"""Strip thinking / chain-of-thought from assistant messages."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm.config import LLMConfig
from shared.llm.providers.openai import OpenAIProvider
from shared.llm.thinking import (
    extract_assistant_text,
    openai_thinking_kwargs,
    strip_thinking,
)

NEMOTRON_CONTENT = """Here's a thinking process:

1.  **Analyze User Input:**
   - User said: "hallo, wie geht es dir"

5.  **Final Output Generation:**
   - Output: "Hallo! Mir geht es gut, danke der Nachfrage. Wie kann ich dir helfen?"✅
</think>Hallo! Mir geht es gut, danke der Nachfrage. Wie kann ich dir helfen?"""


def test_nemotron_closing_tag_without_opener():
    assert strip_thinking(NEMOTRON_CONTENT) == (
        "Hallo! Mir geht es gut, danke der Nachfrage. Wie kann ich dir helfen?"
    )


def test_qwen_think_block():
    raw = "<think>\nIch überlege.\n</think>\nTHOUGHT: fertig\nACTION: FINAL_ANSWER({})"
    assert strip_thinking(raw) == "THOUGHT: fertig\nACTION: FINAL_ANSWER({})"


def test_no_thinking_unchanged():
    assert strip_thinking("Hallo, wie geht's?") == "Hallo, wie geht's?"


def test_empty_and_none_content():
    assert strip_thinking("") == ""
    assert extract_assistant_text(None) == ""
    assert extract_assistant_text({"content": None}) == ""


def test_extract_prefers_content_after_think():
    msg = {"content": NEMOTRON_CONTENT, "reasoning": None}
    assert extract_assistant_text(msg).startswith("Hallo!")


def test_extract_reasoning_content_fallback():
    msg = {"content": None, "reasoning_content": "<think>x</think>Antwort"}
    assert extract_assistant_text(msg) == "Antwort"


def test_extract_content_parts_list():
    msg = {"content": [{"type": "text", "text": "<think>x</think>Hi"}]}
    assert extract_assistant_text(msg) == "Hi"


def test_openai_thinking_kwargs_off_by_default():
    assert openai_thinking_kwargs(False) == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert openai_thinking_kwargs(True) == {
        "chat_template_kwargs": {"enable_thinking": True}
    }


def test_thinking_env_off_by_default(monkeypatch):
    monkeypatch.delenv("THINKING", raising=False)
    assert LLMConfig.from_env().enable_thinking is False


@pytest.mark.parametrize("raw", ["true", "TRUE", " true", "1", "yes", "on"])
def test_thinking_env_true(monkeypatch, raw):
    monkeypatch.setenv("THINKING", raw)
    assert LLMConfig.from_env().enable_thinking is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", ""])
def test_thinking_env_false(monkeypatch, raw):
    monkeypatch.setenv("THINKING", raw)
    assert LLMConfig.from_env().enable_thinking is False


@pytest.mark.anyio
@patch("shared.llm.providers.openai.httpx.AsyncClient")
async def test_openai_chat_sends_enable_thinking_false(mock_client_cls):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "hallo"}}],
        "usage": {},
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    provider = OpenAIProvider(
        LLMConfig(
            openai_api_key="sk-test",
            openai_base_url="https://example.test/v1",
            enable_thinking=False,
        )
    )
    await provider.chat([{"role": "user", "content": "hallo"}], model="nemotron")
    body = mock_client.post.call_args.kwargs["json"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def _mock_client(mock_client_cls, responses):
    mock_client = AsyncMock()
    mock_client.post.side_effect = responses
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client
    return mock_client


def _ok_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _mistral_reject_response():
    import httpx

    request = httpx.Request("POST", "https://mistral.test/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        text='{"error": "chat_template is not supported for Mistral tokenizers."}',
    )
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400", request=request, response=response
    )
    return resp


@pytest.mark.anyio
@patch("shared.llm.providers.openai.httpx.AsyncClient")
async def test_openai_chat_retries_without_template_kwargs_for_mistral(mock_client_cls):
    ok = {"choices": [{"message": {"content": "hallo"}}], "usage": {}}
    mock_client = _mock_client(
        mock_client_cls,
        [_mistral_reject_response(), _ok_response(ok), _ok_response(ok)],
    )
    provider = OpenAIProvider(
        LLMConfig(openai_api_key="sk-test", openai_base_url="https://mistral.test/v1")
    )

    assert await provider.chat([{"role": "user", "content": "hi"}], model="ministral-3-8b") == "hallo"
    first, retry = (c.kwargs["json"] for c in mock_client.post.call_args_list)
    assert "chat_template_kwargs" in first
    assert "chat_template_kwargs" not in retry

    # Remembered: the next call goes out without the kwargs straight away.
    await provider.chat([{"role": "user", "content": "hi"}], model="ministral-3-8b")
    assert mock_client.post.call_count == 3
    assert "chat_template_kwargs" not in mock_client.post.call_args.kwargs["json"]


@pytest.mark.anyio
@patch("shared.llm.providers.openai.httpx.AsyncClient")
async def test_openai_embed_sends_float_encoding_format(mock_client_cls):
    mock_client = _mock_client(
        mock_client_cls, [_ok_response({"data": [{"embedding": [0.1, 0.2]}]})]
    )
    provider = OpenAIProvider(
        LLMConfig(openai_api_key="sk-test", openai_base_url="https://example.test/v1")
    )
    assert await provider.embed("text", model="bge-m3") == [0.1, 0.2]
    assert mock_client.post.call_args.kwargs["json"]["encoding_format"] == "float"

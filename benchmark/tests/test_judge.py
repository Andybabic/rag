import asyncio
import json

import httpx
import judge as judge_mod
import pytest
from settings import Settings

GRADE = {
    "verdict": "partial",
    "refused": False,
    "facts_present": [True, False],
    "extra_claims": 2,
    "contradictions": 0,
    "reason": "ok",
}


def _run(settings: Settings, reply, tmp_path, monkeypatch):
    """Grade one answer against a fake endpoint; return (request, result)."""
    monkeypatch.setattr(judge_mod, "CACHE_DIR", tmp_path)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=reply)

    async def go():
        j = judge_mod.Judge(settings)
        await j.client.aclose()
        j.client = httpx.AsyncClient(
            headers=seen.setdefault("init_headers", dict(j.client.headers)),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await j.grade("Q?", "Gold", ["a", "b"], "Antwort")
        finally:
            await j.aclose()

    return seen, asyncio.run(go())


def test_openai_provider(tmp_path, monkeypatch):
    s = Settings(judge_provider="openai", judge_model="m", judge_api_key="k")
    reply = {"choices": [{"message": {"content": json.dumps(GRADE)}}]}
    seen, result = _run(s, reply, tmp_path, monkeypatch)
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer k"
    assert seen["body"]["temperature"] == 0
    assert result["judge_score"] == 0.5 and result["fact_recall"] == 0.5


def test_ollama_provider(tmp_path, monkeypatch):
    s = Settings(
        judge_provider="ollama",
        judge_model="qwen3:32b",
        judge_base_url="http://gpu:11434/v1",
        judge_api_key="gw",
        judge_num_ctx=8192,
    )
    content = "<think>{not json}</think>" + json.dumps(GRADE)
    seen, result = _run(s, {"message": {"content": content}}, tmp_path, monkeypatch)
    assert seen["url"] == "http://gpu:11434/api/chat"
    assert seen["headers"]["x-api-key"] == "gw"
    assert "authorization" not in seen["headers"]
    body = seen["body"]
    assert body["stream"] is False and body["format"] == "json" and body["think"] is False
    assert body["options"] == {"temperature": 0, "num_ctx": 8192}
    assert result["judge_verdict"] == "partial" and result["extra_claims"] == 2


def test_unknown_provider_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_mod, "CACHE_DIR", tmp_path)
    with pytest.raises(judge_mod.JudgeConfigError):
        judge_mod.Judge(Settings(judge_provider="azure", judge_model="m"))


@pytest.mark.parametrize(
    ("content", "ok"),
    [('{"ok": true}', True), ('{"ok": false}', False), ("Hallo!", False)],
)
def test_ping(content, ok, tmp_path, monkeypatch):
    monkeypatch.setattr(judge_mod, "CACHE_DIR", tmp_path)
    reply = {"message": {"content": content}}

    async def go():
        j = judge_mod.Judge(Settings(judge_provider="ollama", judge_model="m"))
        await j.client.aclose()
        j.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))
        )
        try:
            return await j.ping()
        finally:
            await j.aclose()

    if ok:
        assert asyncio.run(go()) >= 0
    else:
        with pytest.raises(judge_mod.JudgeError):
            asyncio.run(go())


@pytest.mark.parametrize(
    ("provider", "base", "url"),
    [
        (
            "openai",
            "https://api.deepseek.com/chat/completions",
            "https://api.deepseek.com/chat/completions",
        ),
        ("openai", "https://api.deepseek.com/", "https://api.deepseek.com/chat/completions"),
        ("openai", "https://api.deepseek.com/chat", "https://api.deepseek.com/chat/completions"),
        ("openai", "https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("ollama", "http://gpu:11434/api/chat", "http://gpu:11434/api/chat"),
    ],
)
def test_base_url_tolerates_full_endpoint(provider, base, url, tmp_path, monkeypatch):
    monkeypatch.setattr(judge_mod, "CACHE_DIR", tmp_path)
    j = judge_mod.Judge(Settings(judge_provider=provider, judge_model="m", judge_base_url=base))
    assert j.url == url
    asyncio.run(j.client.aclose())

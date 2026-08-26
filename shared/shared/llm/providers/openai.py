"""OpenAI / OpenAI-compatible backend.

Works with api.openai.com, Azure-OpenAI proxies, vLLM, LiteLLM,
LM Studio etc. — anything that speaks the ``/v1/chat/completions``
and ``/v1/embeddings`` shape.

Set ``OPENAI_BASE_URL`` to point at a different host. ``OPENAI_API_KEY``
is required.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from shared.llm.base import LLMProvider
from shared.llm.errors import LLMUnavailableError
from shared.llm.images import to_data_uri
from shared.llm.registry import register
from shared.llm.thinking import extract_assistant_text, openai_thinking_kwargs


def normalize_openai_base_url(url: str) -> str:
    """Append ``/v1`` when the base URL has no path.

    OpenAI-compatible servers (vLLM, LiteLLM, NIM) expose
    ``/v1/chat/completions``. A host-only URL would otherwise hit
    ``/chat/completions`` and 404. Existing paths (``/v1``, Azure
    ``/openai/deployments/...``) are left unchanged.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return raw
    path = urlparse(raw).path
    if path in ("", "/"):
        return f"{raw}/v1"
    return raw


def _normalize_images(messages: list[dict]) -> list[dict]:
    """Convert any message carrying an ``images`` list into OpenAI's
    multimodal ``content`` array (text part + image_url parts). Messages
    without images pass through unchanged."""
    out: list[dict] = []
    for m in messages:
        images = m.get("images")
        if not images:
            out.append(m)
            continue
        parts: list[dict] = []
        if m.get("content"):
            parts.append({"type": "text", "text": m["content"]})
        for img in images:
            parts.append({"type": "image_url", "image_url": {"url": to_data_uri(img)}})
        out.append({"role": m.get("role", "user"), "content": parts})
    return out


@register("openai")
class OpenAIProvider(LLMProvider):
    def _headers(self) -> dict[str, str]:
        if not self.config.openai_api_key:
            raise LLMUnavailableError(
                "OPENAI_API_KEY is required when using the openai provider"
            )
        return {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{normalize_openai_base_url(self.config.openai_base_url)}{path}"

    def _http_error(self, exc: httpx.HTTPStatusError) -> LLMUnavailableError:
        return LLMUnavailableError(
            f"OpenAI returned {exc.response.status_code} at {exc.request.url}: "
            f"{exc.response.text}"
        )

    async def chat(self, messages: list[dict], *, model: str, **opts) -> str:
        body: dict = {
            "model": model,
            "messages": _normalize_images(messages),
            "stream": False,
        }
        options = opts.get("options") or {}
        if "temperature" in options:
            body["temperature"] = options["temperature"]
        else:
            body["temperature"] = 0.2
        if "max_tokens" in options:
            body["max_tokens"] = options["max_tokens"]
        body.update(openai_thinking_kwargs(self.config.enable_thinking))

        try:
            async with httpx.AsyncClient(timeout=self.config.chat_timeout) as client:
                resp = await client.post(
                    self._url("/chat/completions"),
                    headers=self._headers(),
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                self.last_token_counts = {
                    "prompt_eval_count": usage.get("prompt_tokens", 0) or 0,
                    "eval_count": usage.get("completion_tokens", 0) or 0,
                }
                message = (data.get("choices") or [{}])[0].get("message") or {}
                return extract_assistant_text(message)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable / timed out at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise self._http_error(exc) from exc

    async def embed(self, text: str, *, model: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    self._url("/embeddings"),
                    headers=self._headers(),
                    json={"model": model, "input": text},
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable / timed out at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise self._http_error(exc) from exc

    async def list_models(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    self._url("/models"),
                    headers=self._headers(),
                )
                resp.raise_for_status()
                # Normalize to the Ollama-style ``{"name": ...}`` shape so
                # callers don't need to branch on provider.
                data = resp.json().get("data", [])
                return [{"name": item.get("id"), **item} for item in data]
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable / timed out at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise self._http_error(exc) from exc

    async def vision_describe(
        self,
        prompt: str,
        image_base64: str,
        *,
        model: str,
    ) -> str:
        # OpenAI vision uses chat-completions with an image_url content
        # part. We assume PNG; callers already strip data URI prefixes.
        data_uri = f"data:image/png;base64,{image_base64}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ]
        try:
            async with httpx.AsyncClient(timeout=self.config.vision_timeout) as client:
                resp = await client.post(
                    self._url("/chat/completions"),
                    headers=self._headers(),
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        **openai_thinking_kwargs(self.config.enable_thinking),
                    },
                )
                resp.raise_for_status()
                message = (resp.json().get("choices") or [{}])[0].get("message") or {}
                return extract_assistant_text(message)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable / timed out at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise self._http_error(exc) from exc

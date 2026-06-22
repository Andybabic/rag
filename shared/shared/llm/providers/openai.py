"""OpenAI / OpenAI-compatible backend.

Works with api.openai.com, Azure-OpenAI proxies, vLLM, LiteLLM,
LM Studio etc. — anything that speaks the ``/v1/chat/completions``
and ``/v1/embeddings`` shape.

Set ``OPENAI_BASE_URL`` to point at a different host. ``OPENAI_API_KEY``
is required.
"""

from __future__ import annotations

import httpx

from shared.llm.base import LLMProvider
from shared.llm.errors import LLMUnavailableError
from shared.llm.images import to_data_uri
from shared.llm.registry import register


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
        return f"{self.config.openai_base_url.rstrip('/')}{path}"

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

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    self._url("/chat/completions"),
                    headers=self._headers(),
                    json=body,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"OpenAI returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

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
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"OpenAI returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

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
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"OpenAI returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

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
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self._url("/chat/completions"),
                    headers=self._headers(),
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"OpenAI not reachable at {self.config.openai_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"OpenAI returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

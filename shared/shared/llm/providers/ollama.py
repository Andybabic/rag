"""Ollama backend.

Talks to a stock Ollama server or to the Ollama API Key Manager
gateway. Authentication is enabled by setting ``OLLAMA_API_KEY`` —
the value is sent as ``X-API-Key`` per the gateway's spec.
"""

from __future__ import annotations

import httpx

from shared.llm.base import LLMProvider
from shared.llm.errors import LLMUnavailableError
from shared.llm.registry import register


@register("ollama")
class OllamaProvider(LLMProvider):
    def _headers(self) -> dict[str, str]:
        if self.config.ollama_api_key:
            return {"X-API-Key": self.config.ollama_api_key}
        return {}

    def _url(self, path: str) -> str:
        return f"{self.config.ollama_base_url.rstrip('/')}{path}"

    async def chat(self, messages: list[dict], *, model: str, **opts) -> str:
        options = {"temperature": 0.2}
        options.update(opts.get("options") or {})
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    self._url("/api/chat"),
                    headers=self._headers(),
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": options,
                    },
                )
                resp.raise_for_status()
                return resp.json()["message"]["content"]
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"Ollama not reachable at {self.config.ollama_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

    async def embed(self, text: str, *, model: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    self._url("/api/embeddings"),
                    headers=self._headers(),
                    json={"model": model, "prompt": text},
                )
                resp.raise_for_status()
                return resp.json()["embedding"]
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"Ollama not reachable at {self.config.ollama_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

    async def list_models(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    self._url("/api/tags"),
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json().get("models", [])
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"Ollama not reachable at {self.config.ollama_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

    async def vision_describe(
        self,
        prompt: str,
        image_base64: str,
        *,
        model: str,
    ) -> str:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self._url("/api/generate"),
                    headers=self._headers(),
                    json={
                        "model": model,
                        "prompt": prompt,
                        "images": [image_base64],
                        "stream": False,
                    },
                )
                resp.raise_for_status()
                return resp.json().get("response", "").strip()
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"Ollama not reachable at {self.config.ollama_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

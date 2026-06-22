"""Ollama backend.

Talks to a stock Ollama server or to the Ollama API Key Manager
gateway. Authentication is enabled by setting ``OLLAMA_API_KEY`` —
the value is sent as ``X-API-Key`` per the gateway's spec.
"""

from __future__ import annotations

import httpx

from shared.llm.base import LLMProvider
from shared.llm.errors import LLMUnavailableError
from shared.llm.images import to_raw_base64
from shared.llm.registry import register


def _normalize_images(messages: list[dict]) -> list[dict]:
    """Ollama wants raw base64 in a message's ``images`` array — strip any
    data-URI prefix. Messages without images pass through unchanged."""
    out: list[dict] = []
    for m in messages:
        if m.get("images"):
            m = {**m, "images": [to_raw_base64(img) for img in m["images"]]}
        out.append(m)
    return out


@register("ollama")
class OllamaProvider(LLMProvider):
    def _headers(self) -> dict[str, str]:
        if self.config.ollama_api_key:
            return {"X-API-Key": self.config.ollama_api_key}
        return {}

    def _url(self, path: str) -> str:
        return f"{self.config.ollama_base_url.rstrip('/')}{path}"

    async def chat(self, messages: list[dict], *, model: str, **opts) -> str:
        options = {"temperature": 0.2, "num_ctx": self.config.ollama_num_ctx}
        options.update(opts.get("options") or {})
        # Unified knob: callers pass `max_tokens`; Ollama uses `num_predict`.
        if "max_tokens" in options and "num_predict" not in options:
            options["num_predict"] = options.pop("max_tokens")
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    self._url("/api/chat"),
                    headers=self._headers(),
                    json={
                        "model": model,
                        "messages": _normalize_images(messages),
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
        # Ollama-Doku: Vision-Modelle erwarten Bilder im /api/chat-Endpoint
        # *innerhalb* des message-Objekts. Der frühere /api/generate-Pfad
        # mit images im Top-Level wurde von einigen VL-Modellen ignoriert,
        # sodass nur der Text-Prompt verarbeitet wurde.
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    self._url("/api/chat"),
                    headers=self._headers(),
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "user",
                                "content": prompt,
                                "images": [image_base64],
                            }
                        ],
                        "stream": False,
                        "options": {
                            "temperature": 0.2,
                            "num_ctx": self.config.ollama_num_ctx,
                        },
                    },
                )
                resp.raise_for_status()
                return (
                    resp.json().get("message", {}).get("content", "").strip()
                )
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"Ollama not reachable at {self.config.ollama_base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailableError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text}"
            ) from exc

# LLM Provider Pipeline

Zentraler Einstiegspunkt für Chat, Embeddings und Vision. Jeder Service
(cleaning, embedding, evaluation) ruft hierdurch — der Backend-Provider
(Ollama, OpenAI, ...) wird per Env-Var ausgewählt.

```
services/*  ──►  shared.llm.{chat,embed,vision_describe,list_models}
                          │
                          ▼
                 LLMConfig.from_env()
                          │
                          ▼
              registry.get_provider(name)
                  │            │
                  ▼            ▼
             OllamaProvider  OpenAIProvider   ...
```

## Konfiguration

Pro Rolle einzeln umschaltbar (`ollama` | `openai`):

| Env Var               | Default                       | Wofür                              |
|-----------------------|-------------------------------|------------------------------------|
| `LLM_PROVIDER`        | `ollama`                      | Chat (evaluation service)          |
| `EMBEDDING_PROVIDER`  | `ollama`                      | Embeddings (embedding service)     |
| `VISION_PROVIDER`     | `ollama`                      | Bildbeschreibung (cleaning service)|
| `OLLAMA_BASE_URL`     | `http://localhost:11434`      | Ollama-Host                        |
| `OLLAMA_API_KEY`      | *(leer)*                      | Optional: `X-API-Key` Header für Ollama API Key Manager Gateway |
| `OPENAI_BASE_URL`     | `https://api.openai.com/v1`   | Auch für Azure / vLLM / LiteLLM / LM Studio |
| `OPENAI_API_KEY`      | *(leer)*                      | Pflicht wenn ein `*_PROVIDER=openai` |

Modellnamen kommen weiter aus den bestehenden Vars (`LLM_MODEL`,
`EMBEDDING_MODEL`, `VISION_MODEL`) — sie werden 1:1 an den Provider
durchgereicht.

## Beispiel-Setups

**Default (alles Ollama, ohne Auth):**
```env
LLM_PROVIDER=ollama
EMBEDDING_PROVIDER=ollama
VISION_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
```

**Ollama API Key Manager Gateway (mit `X-API-Key`):**
```env
LLM_PROVIDER=ollama
EMBEDDING_PROVIDER=ollama
VISION_PROVIDER=ollama
OLLAMA_BASE_URL=http://gateway.intern:8080/gim-ollama
OLLAMA_API_KEY=ak_...
```

**Chat über OpenAI, Embeddings/Vision weiter Ollama:**
```env
LLM_PROVIDER=openai
EMBEDDING_PROVIDER=ollama
VISION_PROVIDER=ollama
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

**Komplett OpenAI-kompatibler Proxy (Azure / vLLM / LiteLLM):**
```env
LLM_PROVIDER=openai
EMBEDDING_PROVIDER=openai
VISION_PROVIDER=openai
OPENAI_BASE_URL=http://litellm.intern:4000/v1
OPENAI_API_KEY=sk-internal
```

## Verwendung im Service-Code

Direkter Import — kein Provider-Branching nötig:

```python
from shared.llm import chat, embed, vision_describe, list_models, LLMUnavailableError

answer = await chat(messages, model=settings.LLM_MODEL, options={"temperature": 0.2})
vector = await embed(text, model=settings.EMBEDDING_MODEL)
desc   = await vision_describe(prompt, base64_png, model=settings.VISION_MODEL)
models = await list_models()
```

Fehler werden einheitlich als `LLMUnavailableError` (alias
`OllamaUnavailableError` für Rückwärtskompatibilität) geworfen — egal
welcher Provider aktiv ist.

## Neuen Provider hinzufügen

Es gibt **eine** Stelle die du anfasst:

1. Neue Datei `shared/shared/llm/providers/<name>.py` anlegen:

   ```python
   from shared.llm.base import LLMProvider
   from shared.llm.errors import LLMUnavailableError
   from shared.llm.registry import register

   @register("mein-provider")
   class MyProvider(LLMProvider):
       async def chat(self, messages, *, model, **opts) -> str:
           ...
       async def embed(self, text, *, model) -> list[float]:
           ...
       async def vision_describe(self, prompt, image_base64, *, model) -> str:
           ...
       async def list_models(self) -> list[dict]:
           ...
   ```

   Methoden, die der Backend nicht unterstützt, weglassen — die
   Basisklasse wirft dann automatisch `NotImplementedError` mit
   sprechender Nachricht.

2. Provider in `shared/shared/llm/providers/__init__.py` importieren,
   damit der `@register`-Decorator beim Modul-Load greift:

   ```python
   from shared.llm.providers import mein_provider as _mein  # noqa: F401
   ```

3. Falls dein Provider neue Config-Felder braucht (Base-URL, Token),
   in `shared/shared/llm/config.py` an `LLMConfig` ergänzen und in
   `from_env()` aus dem Environment lesen.

Aktivieren per `LLM_PROVIDER=mein-provider` (bzw. `EMBEDDING_PROVIDER`
/ `VISION_PROVIDER`). Kein Service muss angefasst werden.

## Testing

Die Services haben Shims in `services/*/core/` die nur an
`shared.llm` durchreichen. Public-API (z.B. `embed_text`,
`call_llm`, `generate_alt_text`, `OllamaUnavailableError`) bleibt
stabil, deshalb funktionieren bestehende Router-Tests unverändert.

Wer den HTTP-Layer mocken will, patcht jetzt am Provider:

```python
@patch("shared.llm.providers.ollama.httpx.AsyncClient")
async def test_xyz(mock_client_cls):
    ...
```

oder für OpenAI:

```python
@patch("shared.llm.providers.openai.httpx.AsyncClient")
```

## Lokale Installation

`shared` ist ein editable-Package. Nach dem Pull aus diesem Repo
einmal neu installieren, sonst zeigt der editable-Link evtl. auf ein
anderes Repo:

```bash
python -m pip install -e shared
```

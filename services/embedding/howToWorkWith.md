# Embedding Service – Entwickler-Leitfaden

## Aufgabe des Service

Der Embedding Service generiert Vektorembeddings ueber die Ollama-API.
Er wird sowohl bei der Ingestion (Batch-Embedding aller Chunks) als auch
bei Queries (Embedding der Suchanfrage) aufgerufen.

---

## Verzeichnisstruktur

```
services/embedding/
├── main.py              # FastAPI-App, Middleware, Health-Endpoint
├── config.py            # Pydantic-Settings (Ollama-URL, Modell, Batch-Groesse)
├── models.py            # EmbedRequest, BatchEmbedRequest, etc.
├── router/
│   └── v1.py            # Endpunkte: /v1/embed, /v1/embed/batch, /v1/models
├── core/
│   └── ollama.py        # HTTP-Client fuer Ollama-Embeddings
└── tests/
    └── ...
```

---

## Ollama-Client erweitern

Der Client in `core/ollama.py` kommuniziert mit der Ollama-REST-API.

### Bestehende Methoden

| Methode | Beschreibung |
|---------|--------------|
| `embed_text(text, model)` | Einzelnes Embedding via `POST /api/embeddings` |
| `embed_batch(texts, batch_size)` | Mehrere Texte in Batches embedden |
| `list_models()` | Verfuegbare Modelle via `GET /api/tags` |

### Neuen Provider hinzufuegen

Wenn ein anderer Embedding-Provider (z.B. OpenAI, Cohere) unterstuetzt
werden soll:

1. Neue Client-Klasse in `core/` erstellen (z.B. `core/openai_embed.py`).
2. Gleiche Schnittstelle wie `ollama.py` bereitstellen:
   - `embed_text(text, model) -> list[float]`
   - `embed_batch(texts, batch_size) -> list[list[float]]`
3. In `router/v1.py` den Provider per Config auswaehlen:

```python
from config import settings

if settings.EMBEDDING_PROVIDER == "openai":
    from core.openai_embed import embed_text, embed_batch
else:
    from core.ollama import embed_text, embed_batch
```

4. Neue Env-Variablen in `config.py` ergaenzen.

### Fehlerbehandlung

`OllamaUnavailableError` wird bei Connection- oder HTTP-Fehlern geworfen.
Der globale Exception-Handler in `main.py` wandelt sie in eine 503-Response
um. Bei einem neuen Provider ein analoges Custom-Exception definieren und
in `shared/errors.py` die `_STATUS_MAP` erweitern.

---

## Neues Embedding-Modell verwenden

1. Modell in Ollama herunterladen:
   ```bash
   docker exec ollama ollama pull nomic-embed-text
   ```

2. Env-Variable setzen:
   ```
   EMBEDDING_MODEL=nomic-embed-text
   EMBEDDING_DIMENSION=768
   ```

3. **Wichtig:** `EMBEDDING_DIMENSION` muss zur tatsaechlichen Vektordimension
   des Modells passen, da Qdrant die Collections mit dieser Dimension anlegt.

---

## Batch-Verarbeitung anpassen

Der Batch-Endpunkt `/v1/embed/batch`:

- Nimmt eine Liste von Chunks entgegen.
- Teilt sie in Batches der Groesse `EMBED_BATCH_SIZE` auf.
- Verarbeitet jeden Batch sequentiell (Ollama-API ist nicht thread-safe).
- Einzelne Fehler werden gesammelt, aber stoppen nicht den gesamten Batch.

Um die Batch-Groesse zu aendern: `EMBED_BATCH_SIZE` in `.env` setzen.

---

## Endpunkt hinzufuegen

### 1. Modell definieren (models.py)

```python
class SimilarityRequest(BaseModel):
    text_a: str
    text_b: str

class SimilarityResponse(BaseModel):
    score: float
    request_id: str = ""
```

### 2. Route hinzufuegen (router/v1.py)

```python
@router.post("/v1/similarity", response_model=SimilarityResponse)
async def similarity(body: SimilarityRequest, request: Request):
    request_id = getattr(request.state, "request_id", "")
    vec_a = await embed_text(body.text_a, settings.EMBEDDING_MODEL)
    vec_b = await embed_text(body.text_b, settings.EMBEDDING_MODEL)
    score = cosine_similarity(vec_a, vec_b)
    return SimilarityResponse(score=score, request_id=request_id)
```

---

## Tests ausfuehren

```bash
cd services/embedding
python -m pytest tests/ -v
```

Tests mocken die Ollama-API mit `unittest.mock.AsyncMock`, sodass kein
laufender Ollama-Server noetig ist.

### Mocking-Beispiel

```python
from unittest.mock import AsyncMock, patch

@pytest.mark.anyio
async def test_embed_single(client):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}

    with patch("core.ollama.httpx.AsyncClient.post", return_value=mock_response):
        resp = await client.post("/v1/embed", json={
            "content": "Testtext",
            "metadata": {"chunk_id": "c1"}
        })
    assert resp.status_code == 200
    assert len(resp.json()["vector"]) == 3
```

---

## Konfiguration (Env-Variablen)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama-Service-URL |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Modellname in Ollama |
| `EMBEDDING_DIMENSION` | `768` | Vektordimension (muss zum Modell passen) |
| `EMBED_BATCH_SIZE` | `50` | Max. Chunks pro Embed-Batch |
| `LOG_LEVEL` | `INFO` | Log-Level |

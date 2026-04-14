# VectorDB Service – Entwickler-Leitfaden

## Aufgabe des Service

Der VectorDB Service ist ein Wrapper um Qdrant. Er speichert Embedding-
Vektoren, fuehrt Similarity-Searches durch und verwaltet Collections.
Er wird sowohl von der Ingestion-Pipeline (Upsert) als auch vom
Evaluation-Service (Search) aufgerufen.

---

## Verzeichnisstruktur

```
services/vectordb/
├── main.py              # FastAPI-App, Middleware, Health-Endpoint
├── config.py            # Pydantic-Settings (Qdrant-Host, Port)
├── models.py            # UpsertRequest, SearchRequest, CrossSearchRequest, etc.
├── router/
│   └── v1.py            # Endpunkte: /v1/upsert, /v1/search, /v1/search/cross, etc.
├── core/
│   └── qdrant.py        # Singleton-Client, CRUD-Operationen
└── tests/
    └── ...
```

---

## Qdrant-Client (core/qdrant.py)

### Singleton-Pattern

Der Client wird einmal initialisiert und global wiederverwendet:

```python
_client: QdrantClient | None = None

def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    return _client
```

Fuer Tests kann `set_client()` einen In-Memory-Client injizieren:

```python
from qdrant_client import QdrantClient
from core.qdrant import set_client

set_client(QdrantClient(":memory:"))
```

### Collection-Auto-Erstellung

`_ensure_collection(name)` prueft, ob eine Collection existiert, und
erstellt sie automatisch mit `COSINE`-Distanz und der konfigurierten
`EMBEDDING_DIMENSION`. Das bedeutet: Beim ersten Upsert in eine neue
Collection wird sie transparent angelegt.

### ID-Konvertierung

`_to_uuid(chunk_id)` konvertiert beliebige String-IDs zu gueltigen UUIDs
via `uuid5(NAMESPACE_DNS, chunk_id)`. Das stellt sicher, dass Qdrant
immer gueltige Point-IDs erhaelt.

---

## Neue Qdrant-Operation hinzufuegen

### Beispiel: Punktzahl-basiertes Loeschen

1. **Core-Funktion** in `core/qdrant.py`:

```python
def delete_by_filter(collection: str, filters: dict) -> int:
    """Loesche Punkte, die dem Filter entsprechen."""
    client = get_client()
    qdrant_filter = _build_filter(filters)
    result = client.delete(
        collection_name=collection,
        points_selector=FilterSelector(filter=qdrant_filter),
    )
    return result.status
```

2. **Modell** in `models.py`:

```python
class DeleteByFilterRequest(BaseModel):
    collection: str
    filters: dict
```

3. **Route** in `router/v1.py`:

```python
@router.post("/v1/delete-by-filter")
async def delete_by_filter_endpoint(
    body: DeleteByFilterRequest, request: Request
):
    request_id = getattr(request.state, "request_id", "")
    status = delete_by_filter(body.collection, body.filters)
    return {"status": status, "request_id": request_id}
```

---

## Filter-System erweitern

Die Funktion `_build_filter()` in `core/qdrant.py` wandelt ein flaches
Dict in Qdrant-`Filter`-Conditions um:

```python
def _build_filter(filters: dict) -> Filter:
    conditions = []
    for key, value in filters.items():
        conditions.append(
            FieldCondition(key=key, match=MatchValue(value=value))
        )
    return Filter(must=conditions)
```

Um komplexere Filter zu unterstuetzen (z.B. Range-Queries, OR-Logik),
diese Funktion erweitern:

```python
# Beispiel: Range-Filter
if isinstance(value, dict) and "gte" in value:
    conditions.append(
        FieldCondition(key=key, range=Range(gte=value["gte"], lte=value.get("lte")))
    )
```

---

## Cross-Collection-Search

`cross_search()` ist fuer Use Cases mit verknuepften Collections gedacht
(z.B. GW St. Poelten: CNC-Steps → Ruest-Data → Material-Info).

**Ablauf:**
1. Similarity-Search in der `primary_collection`.
2. Fuer jedes Ergebnis: `link_key` aus Payload extrahieren.
3. In jeder `linked_collection`: Punkte mit gleichem `link_key` suchen.
4. Verknuepfte Daten an das Ergebnis anhaengen.

Um eine neue Cross-Search-Strategie zu implementieren, `cross_search()`
in `core/qdrant.py` erweitern oder einen separaten Endpunkt anlegen.

---

## Tests ausfuehren

```bash
cd services/vectordb
python -m pytest tests/ -v
```

### Test-Pattern

Tests nutzen einen In-Memory-Qdrant-Client:

```python
@pytest.fixture(autouse=True)
def in_memory_qdrant():
    set_client(QdrantClient(":memory:"))
    yield
```

So koennen Upsert, Search und Collection-Operationen ohne externen
Qdrant-Server getestet werden.

---

## Konfiguration (Env-Variablen)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `QDRANT_HOST` | `qdrant` | Qdrant-Hostname |
| `QDRANT_PORT` | `6333` | Qdrant-Port |
| `EMBEDDING_DIMENSION` | `768` | Vektordimension fuer neue Collections |
| `LOG_LEVEL` | `INFO` | Log-Level |

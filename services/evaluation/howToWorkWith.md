# Evaluation Service – Entwickler-Leitfaden

## Aufgabe des Service

Der Evaluation Service ist das Herzstueck der RAG-Platform. Er orchestriert
den ReAct-Agent, fuehrt Reranking und Evaluation durch, mappt Zitate und
verwaltet Session-Memory. Er kommuniziert mit dem Embedding- und VectorDB-
Service fuer die Suche.

---

## Verzeichnisstruktur

```
services/evaluation/
├── main.py              # FastAPI-App, Middleware, Health-Endpoint
├── config.py            # Pydantic-Settings (LLM, URLs, Agent-Config)
├── models.py            # Alle Request-/Response-Modelle
├── router/
│   └── v1.py            # Endpunkte: /v1/rerank, /v1/evaluate, /v1/citations,
│                        #            /v1/agent/query, /v1/log
├── core/
│   ├── agent.py         # ReAct-Agent-Loop (Hauptlogik)
│   ├── actions.py       # Action-Handler (SEARCH, SEARCH_CNC, CLARIFY, etc.)
│   ├── llm.py           # Ollama-Chat-Client
│   ├── memory.py        # In-Memory Session-Gedaechtnis
│   ├── reranker.py      # Chunk-Reranking nach Score-Threshold
│   ├── evaluator.py     # Chunk-Suffizienz-Pruefung
│   ├── citations.py     # Zitat-Mapping [1], [2] → Chunks
│   └── use_cases.py     # Use-Case-Registry (Prompts, Actions, Collections)
└── tests/
    └── ...
```

---

## Neue Agent-Action erstellen

Dies ist die haeufigste Erweiterung im Evaluation Service.

### 1. Handler implementieren

In `core/actions.py` eine neue async Funktion erstellen:

```python
async def action_search_norm(args: dict, context: dict) -> str:
    """SEARCH_NORM – Suche nach DIN/DVGW-Normen."""
    norm_id = args.get("norm_id", "")
    # context enthaelt: use_case, session_id, collection, embedding_url, vectordb_url

    # Beispiel: Embedding erzeugen und in spezieller Collection suchen
    query_vector = await _embed_query(norm_id, context["embedding_url"])
    results = await _search_vectors(
        "normen_db", query_vector, top_k=3, vectordb_url=context["vectordb_url"]
    )
    return f"Norm {norm_id}: {results}"
```

### 2. Handler registrieren

In `core/actions.py` am Ende der `ACTION_HANDLERS`-Map:

```python
ACTION_HANDLERS["SEARCH_NORM"] = action_search_norm
```

### 3. In Use-Case aktivieren

In `core/use_cases.py` die Action in der `agent_actions`-Liste des
entsprechenden Use Case ergaenzen:

```python
"stadtwerke": {
    "agent_actions": [
        "SEARCH", "SEARCH_NORM", "RECALL_MEMORY",
        "LOOKUP_SOURCES", "FINAL_ANSWER",
    ],
    ...
}
```

Und im zugehoerigen Plugin in `services/data_structure/plugins/`:

```python
def get_agent_actions(self) -> list[str]:
    return ["SEARCH", "SEARCH_NORM", "RECALL_MEMORY", "LOOKUP_SOURCES", "FINAL_ANSWER"]
```

### 4. Tests schreiben

```python
@pytest.mark.anyio
async def test_search_norm_action():
    with patch("core.actions._embed_query", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.1] * 768
        with patch("core.actions._search_vectors", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = [{"text": "DIN EN 1610 ..."}]
            result = await action_search_norm(
                {"norm_id": "DIN EN 1610"},
                {"embedding_url": "http://test", "vectordb_url": "http://test",
                 "use_case": "stadtwerke", "session_id": "s1", "collection": "sw_default"},
            )
    assert "DIN EN 1610" in result
```

---

## Bestehende Actions

| Action | Beschreibung | Use Cases |
|--------|--------------|-----------|
| `SEARCH` | Embedding → VectorDB → Reranking → Top-5-Ergebnisse | alle |
| `SEARCH_CNC` | Cross-Collection-Suche (CNC + Ruest + Material) | gw_stpoelten |
| `CLARIFY` | Rueckfrage an den Benutzer formulieren | wiener_linien |
| `RECALL_MEMORY` | Session-Gedaechtnis lesen | alle |
| `LOOKUP_SOURCES` | Verfuegbare Collections auflisten (nach Prefix gefiltert) | alle |
| `FINAL_ANSWER` | Antwort zurueckgeben (beendet den Agent-Loop) | alle |

---

## ReAct-Agent anpassen

Der Agent in `core/agent.py` folgt dem THOUGHT/ACTION-Muster:

```
THOUGHT: Ich muss nach Wartungsintervallen suchen.
ACTION: SEARCH({"query": "Wartungsintervall Pumpe"})
```

### Agent-Loop aendern

Die Funktion `run_agent()` steuert den Loop:

1. System-Prompt wird aus `use_cases.py` geladen.
2. Verfuegbare Actions werden als Instruktionen eingefuegt.
3. Pro Schritt: LLM aufrufen → THOUGHT/ACTION parsen → Action ausfuehren.
4. Ergebnis als OBSERVATION zurueck in die Konversation.
5. Wiederholen bis `FINAL_ANSWER` oder `max_steps` erreicht.

Um das Parsing zu aendern (z.B. anderes Format), die Regex-Muster in
`agent.py` anpassen.

### Max. Schritte aendern

- Global: `AGENT_MAX_STEPS` in `.env` (Default: 5).
- Per Request: `config.max_steps` im `AgentQueryRequest`.

---

## Neuen Use Case registrieren

In `core/use_cases.py` den Eintrag in `_USE_CASE_META` ergaenzen:

```python
"mein_usecase": {
    "system_prompt": {
        "default": "Du bist ein Assistent fuer ...",
    },
    "agent_actions": ["SEARCH", "RECALL_MEMORY", "LOOKUP_SOURCES", "FINAL_ANSWER"],
    "default_collection": "mu_default",
},
```

**Wichtig:** Die Werte muessen mit dem Plugin im Data-Structure-Service
uebereinstimmen (gleicher Prompt, gleiche Actions, gleiche Collections).

---

## Reranking anpassen

In `core/reranker.py`:

- `rerank_chunks()` sortiert nach Score und teilt in above/below Threshold.
- Relevanz-Labels: `highly_relevant` (>=0.8), `relevant` (>=0.5),
  `marginally_relevant` (>=0.3), `not_relevant` (<0.3).

Um die Reranking-Logik zu aendern (z.B. LLM-basiertes Reranking),
die Funktion erweitern oder eine Alternative erstellen.

---

## Citations-Mapping

In `core/citations.py`:

- Parst `[1]`, `[2]`-Referenzen im Antworttext.
- Mappt 1-indexiert auf die Chunk-Liste.
- Gibt `file_name`, `page`, `excerpt` (erste 200 Zeichen) zurueck.

Um das Mapping zu erweitern (z.B. Seitenbereiche, URLs), die Funktion
`map_citations()` anpassen.

---

## LLM-Client

In `core/llm.py`:

```python
async def call_llm(messages: list[dict], model: str) -> str:
```

- Ruft Ollama `/api/chat` auf.
- Gibt den `content`-String der Antwort zurueck.
- Wirft `LLMUnavailableError` bei Verbindungsproblemen.

Um ein anderes LLM-Backend zu unterstuetzen, eine alternative Funktion
mit gleicher Signatur erstellen und in `core/agent.py` einbinden.

---

## Session-Memory

In `core/memory.py`:

- `read_memory(session_id)` → String mit bisherigem Kontext.
- `write_memory(session_id, memory)` → Speichern (max. `MEMORY_MAX_CHARS`).
- Aktuell In-Memory (Dict). Fuer Persistenz: auf PostgreSQL umstellen
  (Tabelle `agent_memory` existiert bereits im Schema).

---

## Endpunkt hinzufuegen

```python
# models.py
class MyRequest(BaseModel):
    param: str

# router/v1.py
@router.post("/v1/my-endpoint")
async def my_endpoint(body: MyRequest, request: Request):
    request_id = getattr(request.state, "request_id", "")
    result = await my_function(body.param)
    return {"result": result, "request_id": request_id}
```

---

## Tests ausfuehren

```bash
cd services/evaluation
python -m pytest tests/ -v
```

Externe Abhaengigkeiten (Ollama, Embedding-Service, VectorDB-Service)
werden via `unittest.mock.AsyncMock` gemockt.

---

## Konfiguration (Env-Variablen)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama-Service-URL |
| `LLM_MODEL` | `qwen2.5:14b` | Modellname fuer Textgenerierung |
| `EMBEDDING_SERVICE_URL` | – (pflicht) | URL des Embedding Service |
| `VECTORDB_SERVICE_URL` | – (pflicht) | URL des VectorDB Service |
| `DATABASE_URL` | – (pflicht) | PostgreSQL Connection-String |
| `AGENT_MAX_STEPS` | `5` | Max. Schritte im ReAct-Loop |
| `MEMORY_MAX_CHARS` | `4000` | Max. Zeichen im Session-Gedaechtnis |
| `LOG_LEVEL` | `INFO` | Log-Level |

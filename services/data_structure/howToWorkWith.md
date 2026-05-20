# Data Structure Service – Entwickler-Leitfaden

## Aufgabe des Service

Der Data Structure Service zerlegt Markdown-Text in semantische Chunks,
reichert sie mit Metadaten an und routet sie in die richtige Qdrant-Collection.
Die Anreicherung erfolgt ueber ein Plugin-System, das pro Use Case
unterschiedliches Verhalten ermoeglicht.

---

## Verzeichnisstruktur

```
services/data_structure/
├── main.py              # FastAPI-App, Middleware, Health-Endpoint
├── config.py            # Pydantic-Settings (Chunk-Groesse, Overlap)
├── models.py            # StructureRequest, CNCStructureRequest, etc.
├── router/
│   └── v1.py            # Endpunkte: /v1/structure, /v1/structure/cnc, /v1/use-cases
├── core/
│   └── chunker.py       # Markdown-Chunking mit Page-Tracking + Plugin-Enrichment
├── plugins/
│   ├── base.py          # BasePlugin – abstrakte Basisklasse
│   ├── __init__.py      # Plugin-Registry (register_plugin / get_plugin)
│   ├── beispiel.py      # Referenz-Plugin (Vorlage)
│   ├── neumann.py       # Firma Neumann – Maschinenwartung
│   ├── gw_stpoelten.py  # GW St. Poelten – CNC-Fertigung
│   └── wiener_linien.py # Wiener Linien – Wissensassistent
└── tests/
    └── ...
```

---

## Neues Plugin (Use Case) erstellen

Dies ist die haeufigste Erweiterung. Die vollstaendige Anleitung mit
Checkliste ist in `docs/adding_use_case.md`. Hier die Kurzfassung:

### 1. Plugin-Datei anlegen

```bash
cp plugins/beispiel.py plugins/mein_usecase.py
```

### 2. Plugin-Klasse implementieren

```python
from plugins.base import BasePlugin
from shared.models import ChunkMetadata

class MeinUseCasePlugin(BasePlugin):
    use_case_id = "mein_usecase"

    def enrich_metadata(
        self, base: ChunkMetadata, text: str, raw_meta: dict
    ) -> ChunkMetadata:
        # Hier eigene Logik: Kategorie erkennen, Felder extrahieren, etc.
        base.extra = {**raw_meta, "custom_field": "wert"}
        base.collection = "mu_default"
        return base

    def get_collections(self) -> list[str]:
        return ["mu_default", "mu_spezial"]

    def get_system_prompt(self, role: str = "default") -> str:
        return "Du bist ein Assistent fuer ..."

    def get_agent_actions(self) -> list[str]:
        return ["SEARCH", "RECALL_MEMORY", "LOOKUP_SOURCES", "FINAL_ANSWER"]
```

### 3. Plugin registrieren

In `plugins/__init__.py`:

```python
from plugins.mein_usecase import MeinUseCasePlugin
register_plugin(MeinUseCasePlugin())
```

### 4. Evaluation-Service aktualisieren

Den Use Case auch in `services/evaluation/core/use_cases.py` eintragen
(`_USE_CASE_META`), damit System-Prompt und Agent-Actions im Agent
verfuegbar sind.

### 5. Tests schreiben

```python
from plugins.mein_usecase import MeinUseCasePlugin
from shared.models import ChunkMetadata

def test_enrichment():
    plugin = MeinUseCasePlugin()
    meta = ChunkMetadata(
        chunk_id="t1", file_name="test.pdf",
        page=1, doc_type="pdf",
        use_case="mein_usecase", collection="mu_default",
    )
    result = plugin.enrich_metadata(meta, "Beispieltext", {})
    assert result.extra["custom_field"] == "wert"
```

---

## BasePlugin-Methoden

| Methode | Pflicht? | Beschreibung |
|---------|----------|--------------|
| `enrich_metadata(base, text, raw_meta)` | Ja | Analysiert jeden Chunk, fuellt `base.extra`, setzt `base.collection` |
| `get_collections()` | Nein | Liste der Qdrant-Collections. Erster Eintrag = Default |
| `get_system_prompt(role)` | Nein | LLM-System-Prompt, optional rollenbasiert |
| `get_agent_actions()` | Nein | Verfuegbare Actions im ReAct-Loop |

---

## Chunking-Logik anpassen

Der Chunker in `core/chunker.py` nutzt `llama_index.SentenceSplitter`.

### Page-Tracking

Dokumente enthalten `<!-- page:N -->`-Marker, die vom Cleaning Service
eingefuegt werden. Der Chunker:

1. Baut einen `offset → page_number`-Index (`_build_page_index()`).
2. Ordnet jedem Chunk die korrekte Seitenzahl zu (`_page_for_offset()`).
3. Entfernt die Marker aus dem finalen Chunk-Text (`_strip_page_anchors()`).

### Chunk-Groesse aendern

Ueber Env-Variablen oder per Request:

- `DEFAULT_CHUNK_SIZE` (256 **Tokens**) – globaler Default.
  LlamaIndex `SentenceSplitter` zaehlt Tokens, nicht Zeichen.
  Faustregel DE-Text: 1 Token ≈ 3–4 Zeichen, also ~1000 Zeichen pro Chunk.
- `DEFAULT_CHUNK_OVERLAP` (32 Tokens) – globaler Default.
- Per Request: `config.chunk_size` und `config.chunk_overlap`.

### Eigene Chunking-Strategie

Um die Chunking-Logik grundlegend zu aendern, die Funktion
`chunk_markdown()` in `core/chunker.py` anpassen. Die Signatur ist:

```python
def chunk_markdown(
    markdown: str,
    metadata: dict,
    use_case: str,
    plugin: BasePlugin,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
```

---

## CNC-Endpunkt erweitern

Der Endpunkt `POST /v1/structure/cnc` ist speziell fuer den GW-St.-Poelten-
Use-Case. Er nutzt das `GWStPoeltenPlugin` intern und klassifiziert Chunks
in drei Kategorien:

- `cnc_blocks` – G-Code-Bloecke
- `ruest_chunks` – Ruest- und Setup-Daten
- `material_chunks` – Materialdaten

Um eine weitere Spezial-Strukturierung hinzuzufuegen, einen neuen Endpunkt
in `router/v1.py` anlegen, der das entsprechende Plugin direkt nutzt.

---

## Endpunkt hinzufuegen

In `router/v1.py`:

```python
@router.post("/v1/my-endpoint")
async def my_endpoint(body: MyRequest, request: Request):
    request_id = getattr(request.state, "request_id", "")
    # ...
    return {"result": "...", "request_id": request_id}
```

Modelle in `models.py` definieren. Der `request_id` wird automatisch von
der `RequestIDMiddleware` gesetzt.

---

## Tests ausfuehren

```bash
cd services/data_structure
python -m pytest tests/ -v
```

---

## Konfiguration (Env-Variablen)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `DEFAULT_CHUNK_SIZE` | `256` | Chunk-Groesse in **Tokens** (SentenceSplitter; ≈ 1000 Zeichen DE) |
| `DEFAULT_CHUNK_OVERLAP` | `32` | Ueberlappung in **Tokens** (≈ 130 Zeichen DE) |
| `LOG_LEVEL` | `INFO` | Log-Level |

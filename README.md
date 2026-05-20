# RAG Platform

Modulare Retrieval-Augmented-Generation-Plattform für internes Unternehmenswissen.

## Struktur

```
shared/          – Gemeinsame Modelle, Error-Handling, Tracing
services/
  cleaning/      – Dokumentenverarbeitung (PDF, Word, CSV → Markdown)
  data_structure/– Chunking & Metadaten-Anreicherung
  embedding/     – Vektor-Erzeugung via Ollama
  vectordb/      – Qdrant-Anbindung & Ähnlichkeitssuche
  evaluation/    – ReAct-Agent, Reranking, Citation Mapping
  frontend/      – Chat-UI mit Use-Case-Auswahl
docs/            – Architektur- und API-Dokumentation
```

## Schnellstart

```bash
# Shared-Package installieren (editable)
pip install -e ./shared

# Tests ausführen
cd shared && pytest
```

### Entwicklungs-Modi

| Command | Frontend | Backend | Einsatzzweck |
|---|---|---|---|
| `make dev` | Prod-Build im Container (Port 3000) | Docker | „Wie in Produktion" |
| `make dev-fe` | Vite Dev-Server auf Host (Port 5173, Hot-Reload) | Docker | Aktive Frontend-Entwicklung |
| `make frontend-dev` | Vite Dev-Server | — (muss laufen) | Reiner FE-Fokus |

`make dev-fe` startet alle Backend-Container und führt dann `npm run dev`
direkt aus, mit den Service-URLs auf `localhost:<port>` gemappt. Änderungen
an Svelte-/TS-Dateien werden sofort übernommen.

## Use Cases

| Kürzel | Beschreibung |
|---|---|
| `neumann` | Maschinenwartung – Störungsanalyse & Behebungsvorschläge |
| `gw_stpoelten` | CNC-Rüstungsdaten – Werkzeugalternativen |
| `wiener_linien` | Wissensassistent – Vorschriften & Prüfungsvorbereitung |

### Neuen Use Case anlegen

Ein Script legt alle nötigen Einträge in einem Rutsch an – idempotent, mehrfach
aufrufbar:

```bash
python scripts/add_usecase.py <id> [--prefix <p>] [--collection <c>]
                                    [--roles default,trainee]
                                    [--actions SEARCH,FINAL_ANSWER,...]
```

Beispiel:

```bash
python scripts/add_usecase.py ustp --prefix ustp_ --collection ustp_docs
```

Das Script erzeugt / patcht:

1. `services/evaluation/core/prompts/use_cases/<id>/<role>.txt` – System-Prompt
   (Platzhalter, danach von Hand finalisieren)
2. `services/evaluation/core/prompts/use_cases_registry.json` – Rollen,
   Default-Collection und erlaubte Agent-Actions
3. `services/data_structure/plugins/<id>.py` – Plugin-Stub mit
   `enrich_metadata()` für use-case-spezifische Metadaten
4. `services/data_structure/plugins/__init__.py` – Import + Registrierung
5. `services/evaluation/core/use_cases.py` – Collection-Prefix für harte
   Isolation zwischen Use Cases
6. `services/frontend/src/lib/use-cases.ts` – Eintrag für die Use-Case-Auswahl
   im Chat-UI (Label via `--label`, Kurztext via `--description`)

Danach Services neu starten und Ingestion via `POST /v1/structure` mit
`use_case="<id>"` absetzen. Prompt und `enrich_metadata()` können jederzeit
editiert werden.

### Retrieval-Qualität

Die Retrieval-Pipeline nutzt:

- **Chunking mit Breadcrumbs** – jeder Chunk bekommt Header-Hierarchie
  (`H1 > H2 > H3`) als Präfix, damit Embeddings die Dokumentposition erfassen
  (`DEFAULT_CHUNK_SIZE=256` Tokens, Overlap 32 Tokens)
- **Hybrid Search** – dichte Vektorsuche (Qdrant, Embedding via `bge-m3`) +
  BM25 über denselben Kandidaten-Pool, fusioniert via Reciprocal Rank Fusion
- **Cross-Encoder Reranker** – `BAAI/bge-reranker-v2-m3` scored jedes
  (Query, Chunk)-Paar gemeinsam; per `RERANKER_ENABLED=false` abschaltbar
- **Use-Case-Isolation** – Collection-Prefixes verhindern Cross-Use-Case-Zugriff

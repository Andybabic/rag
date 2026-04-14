# Interface Agreement

Dieses Dokument beschreibt die Schnittstellen (Interfaces), uber die die einzelnen Services der Kermit-Plattform miteinander kommunizieren. Es basiert auf der vereinheitlichten Analyse aller sechs Use Cases (RHP, FILL, ESECO, Wiener Linien, GW St. Polten, Neumann) und bildet die vier Phasen der gemeinsamen Pipeline ab.

---

## Ubersicht der Services

| Service            | Port | Technologie | Aufgabe                                      |
| ------------------ | ---- | ----------- | -------------------------------------------- |
| Cleaning Service   | 8001 | FastAPI     | Dokumentenparsing, Textextraktion            |
| Data Structure     | 8002 | FastAPI     | Chunking, Metadaten-Anreicherung via Plugins |
| Embedding Service  | 8003 | FastAPI     | Vektorisierung via Ollama                    |
| VectorDB Service   | 8004 | FastAPI     | Speicherung und Suche in Qdrant              |
| Evaluation Service | 8005 | FastAPI     | ReAct-Agent, Reranking, Antwortgenerierung   |
| Frontend Service   | 3000 | SvelteKit   | UI, Pipeline-Orchestrierung, Admin           |

---

## Gemeinsame Kommunikationsmuster

### Request-Tracing

Alle Services verwenden den `X-Request-ID`-Header. Die `RequestIDMiddleware` (aus `shared/`) erzeugt oder liest diese ID und propagiert sie uber alle Service-Aufrufe hinweg. So lassen sich Anfragen uber die gesamte Pipeline nachverfolgen.

### Fehlerbehandlung

Jeder Service gibt Fehler im einheitlichen `APIError`-Format zuruck:

```json
{
  "error": "ValueError",
  "detail": "Beschreibung des Fehlers",
  "request_id": "abc-123",
  "service": "cleaning"
}
```

HTTP-Status-Mapping: `ValueError` -> 400, `FileNotFoundError` -> 404, `PermissionError` -> 403, alle anderen -> 500.

### HTTP-Clients

Services kommunizieren uber `httpx.AsyncClient` mit folgenden Timeouts:
- **30s** fur schwere Operationen (Embedding, Suche)
- **10s** fur Metadaten-Operationen

### Shared Models (`shared/shared/models.py`)

Diese Datenmodelle bilden den gemeinsamen Vertrag zwischen allen Services:

```
ChunkMetadata:
  chunk_id: str (UUID, auto-generiert)
  file_name: str
  page: Optional[int]
  total_pages: Optional[int]
  doc_type: str              # "pdf", "docx", "csv", "txt", "image"
  use_case: str              # "neumann", "gw_stpoelten", "wiener_linien"
  collection: str            # Ziel-Qdrant-Collection
  extra: dict                # Use-Case-spezifische Metadaten (via Plugin)

Chunk:
  id: str (UUID)
  text: str
  metadata: ChunkMetadata

EmbeddedChunk:
  chunk: Chunk
  vector: list[float]
  model: str
  dimension: int

SearchResult:
  chunk: Chunk
  score: float
```

---

## Phase 1 -- Ingestion & Aufbereitung

Die Ingestion-Pipeline wird vom **Frontend Service** orchestriert (`/api/ingest`) und durchlauft vier Services sequenziell:

```
Datenquelle (Upload)
    |
    v
[1] Cleaning Service (/v1/clean)
    |   Eingabe: Datei (multipart)
    |   Ausgabe: Markdown + Metadaten
    v
[2] Data Structure Service (/v1/structure)
    |   Eingabe: Markdown + Metadaten + Use-Case
    |   Ausgabe: Chunks mit angereicherten Metadaten + Collection-Routing
    v
[3] Embedding Service (/v1/embed/batch)
    |   Eingabe: Chunk-Texte
    |   Ausgabe: Vektoren pro Chunk
    v
[4] VectorDB Service (/v1/upsert)
        Eingabe: Vektoren + Metadaten
        Ausgabe: Bestatigung der Speicherung
```

### Interface 1.1: Frontend -> Cleaning Service

**Endpoint:** `POST /v1/clean`

| Richtung | Feld              | Typ          | Beschreibung                                  |
| -------- | ----------------- | ------------ | --------------------------------------------- |
| Request  | `file`            | UploadFile   | Hochgeladene Datei (multipart/form-data)      |
| Request  | `config`          | JSON (opt.)  | `extract_images: bool`, `pii_removal: bool`   |
| Response | `markdown`        | str          | Strukturierter Markdown-Text                  |
| Response | `images`          | list[dict]   | Extrahierte Bilder (Base64 + Metadaten)       |
| Response | `pages`           | list[dict]   | Seitenweise Aufteilung                        |
| Response | `metadata`        | dict         | `file_name`, `total_pages`, `doc_type`        |

**Batch-Variante:** `POST /v1/clean/batch` -- mehrere Dateien parallel, Antwort enthalt `results[]` und `errors[]`.

**Use-Case-spezifische Cleaning-Schritte:**
- **FILL:** `pii_removal: true` (Anonymisierung von Kundendaten)
- **Neumann:** Bild-Extraktion fur LLM-Vision-Captions
- **RHP:** Standard-Cleaning, Materialnormalisierung erfolgt im nachsten Schritt

### Interface 1.2: Frontend -> Data Structure Service

**Endpoint:** `POST /v1/structure`

| Richtung | Feld              | Typ              | Beschreibung                                |
| -------- | ----------------- | ---------------- | ------------------------------------------- |
| Request  | `markdown`        | str              | Markdown aus Cleaning Service               |
| Request  | `metadata`        | dict             | Metadaten aus Cleaning Service              |
| Request  | `use_case`        | str              | Use-Case-Kennung (z.B. "neumann")           |
| Request  | `config`          | StructureConfig  | `chunk_size`, `chunk_overlap`, `target_collection`, `extra` |
| Response | `chunks`          | list[Chunk]      | Chunks mit angereicherter `ChunkMetadata`   |
| Response | `routing`         | dict             | `{ "collection": str }` -- Ziel-Collection  |
| Response | `total_chunks`    | int              | Anzahl erzeugter Chunks                     |

**Spezial-Endpoint fur CNC (GW St. Polten):** `POST /v1/structure/cnc`

| Richtung | Feld              | Typ              | Beschreibung                                |
| -------- | ----------------- | ---------------- | ------------------------------------------- |
| Request  | `markdown`        | str              | CNC-Programm als Markdown                   |
| Request  | `metadata`        | dict             | Enthalt `product_id`, `ruest_map_id`        |
| Response | `cnc_blocks`      | list[dict]       | Einzelne CNC-Schritte mit Parametern        |
| Response | `ruest_chunks`    | list[Chunk]      | Rustungsdaten-Chunks                        |
| Response | `material_chunks` | list[Chunk]      | Materialinfo-Chunks                         |

**Plugin-System fur Metadaten-Anreicherung:**

| Use Case       | Plugin             | Angereicherte Felder                              | Collections                                    |
| -------------- | ------------------ | ------------------------------------------------- | ---------------------------------------------- |
| Neumann        | NeumannPlugin      | `machine_id`, `area`, `topic`                     | `neumann_machines`, `neumann_{machine_id}`     |
| GW St. Polten  | GWStPoeltenPlugin  | `cnc_step_id`, `operation_type`, `material_class` | `gw_cnc_steps`, `gw_ruest_data`, `gw_material_info` |
| Wiener Linien  | WienerLinienPlugin | `fahrzeug_typ`, `kategorie`                       | `wl_fahrzeug`                                  |

### Interface 1.3: Frontend -> Embedding Service

**Endpoint:** `POST /v1/embed/batch`

| Richtung | Feld              | Typ                  | Beschreibung                              |
| -------- | ----------------- | -------------------- | ----------------------------------------- |
| Request  | `chunks`          | list[BatchChunk]     | `{ type, content, metadata }` pro Chunk   |
| Request  | `batch_size`      | int (opt.)           | Batch-Grosse (Default: 50)                |
| Response | `embeddings`      | list[EmbeddingResult]| `{ chunk_id, vector, model, dimension }`  |
| Response | `total`           | int                  | Anzahl erfolgreicher Embeddings           |
| Response | `failed`          | int                  | Anzahl fehlgeschlagener Embeddings        |

**Einzelnes Embedding:** `POST /v1/embed` -- fur Query-Embedding in Phase 2.

Intern verbindet sich der Embedding Service mit **Ollama** (`/api/embeddings`), Standardmodell: `qwen3-embedding:0.6b`.

### Interface 1.4: Frontend -> VectorDB Service

**Endpoint:** `POST /v1/upsert`

| Richtung | Feld              | Typ                    | Beschreibung                             |
| -------- | ----------------- | ---------------------- | ---------------------------------------- |
| Request  | `collection`      | str                    | Ziel-Collection (aus Routing)            |
| Request  | `embeddings`      | list[EmbeddingPayload] | `{ chunk_id, vector, metadata }`         |
| Response | `upserted`        | int                    | Anzahl eingefugter/aktualisierter Punkte |
| Response | `collection`      | str                    | Betroffene Collection                    |

Collections werden automatisch erstellt, falls sie nicht existieren (Cosine-Distanz).

---

## Phase 2 -- Anfrage & Query-Generierung

Der Query-Flow wird vom **Evaluation Service** uber den ReAct-Agenten gesteuert:

```
User Query (via Frontend)
    |
    v
[1] Evaluation Service (/v1/agent/query)
    |   Agent entscheidet Aktion (SEARCH, CLARIFY, etc.)
    |
    |-- ACTION: SEARCH --------------------------------|
    |   |                                              |
    |   v                                              v
    |   [2a] Embedding Service        [2b] VectorDB Service
    |        (/v1/embed)                   (/v1/search)
    |        Query -> Vektor               Vektor -> Ergebnisse
    |                                              |
    |   <------------------------------------------+
    |
    |-- ACTION: SEARCH_CNC (nur GW St. Polten) -------|
    |   |                                              |
    |   v                                              v
    |   [2a] Embedding Service        [2b] VectorDB Service
    |        (/v1/embed)                   (/v1/search/cross)
    |                                      Cross-Collection-Suche
    |   <------------------------------------------+
    |
    v
    Ergebnisse -> weiter zu Phase 3
```

### Interface 2.1: Frontend -> Evaluation Service (Agent)

**Endpoint:** `POST /v1/agent/query`

| Richtung | Feld          | Typ                | Beschreibung                              |
| -------- | ------------- | ------------------ | ----------------------------------------- |
| Request  | `query`       | str                | Benutzeranfrage                           |
| Request  | `use_case`    | str                | Use-Case-Kennung                          |
| Request  | `session_id`  | str (UUID)         | Sitzungs-ID fur Kontexterhaltung          |
| Request  | `role`        | str                | Benutzerrolle ("default", "trainee")      |
| Request  | `config`      | AgentConfig        | `collection`, `filters`, `max_steps`      |
| Request  | `history`     | list[HistoryMsg]   | Bisheriger Chatverlauf                    |
| Response | `answer`      | str                | Generierte Antwort                        |
| Response | `sufficient`  | bool               | Ob genugend Kontext vorhanden war         |
| Response | `agent_steps` | list[dict]         | Durchlaufene Agent-Schritte               |

**Use-Case-spezifische Query-Aufbereitung:**
- **Wiener Linien:** Rollenauswahl (trainee = vereinfachte Sprache)
- **GW St. Polten:** CNC-Code-Analyse uber SEARCH_CNC
- **FILL:** Metadaten-Filter (Kunde, Anlagentyp)

### Interface 2.2: Evaluation Service -> Embedding Service

**Endpoint:** `POST /v1/embed`

| Richtung | Feld      | Typ   | Beschreibung                     |
| -------- | --------- | ----- | -------------------------------- |
| Request  | `content` | str   | Query-Text zum Vektorisieren     |
| Request  | `type`    | str   | "text" (Default)                 |
| Response | `vector`  | list[float] | Embedding-Vektor             |
| Response | `model`   | str   | Verwendetes Modell               |
| Response | `dimension` | int | Vektor-Dimension                 |

### Interface 2.3: Evaluation Service -> VectorDB Service (Suche)

**Standard-Suche:** `POST /v1/search`

| Richtung | Feld          | Typ         | Beschreibung                         |
| -------- | ------------- | ----------- | ------------------------------------ |
| Request  | `collection`  | str         | Zu durchsuchende Collection          |
| Request  | `vector`      | list[float] | Query-Embedding                      |
| Request  | `top_k`       | int         | Maximale Ergebnisanzahl (Default: 5) |
| Request  | `filters`     | dict        | Feld:Wert-Filter (Qdrant FieldCondition) |
| Response | `results`     | list[dict]  | `{ chunk_id, score, text, metadata }` |
| Response | `total`       | int         | Anzahl Treffer                       |

**Cross-Collection-Suche (GW St. Polten):** `POST /v1/search/cross`

| Richtung | Feld                   | Typ         | Beschreibung                                      |
| -------- | ---------------------- | ----------- | ------------------------------------------------- |
| Request  | `primary_collection`   | str         | Haupt-Collection (z.B. `gw_cnc_steps`)            |
| Request  | `linked_collections`   | list[str]   | Verknupfte Collections (`gw_ruest_data`, `gw_material_info`) |
| Request  | `vector`               | list[float] | Query-Embedding                                   |
| Request  | `link_key`             | str         | Verknupfungsfeld (z.B. `cnc_step_id`)             |
| Request  | `top_k`                | int         | Maximale Ergebnisanzahl                            |
| Response | `results`              | list[dict]  | `{ primary: SearchResult, linked: { collection: SearchResult } }` |

---

## Phase 3 -- Ranking & Agent-Loop

Phase 3 lauft vollstandig innerhalb des **Evaluation Service** ab. Der ReAct-Agent entscheidet iterativ, ob die Ergebnisse ausreichen:

```
Suchergebnisse aus Phase 2
    |
    v
[1] Reranking (lokal im Evaluation Service)
    |   Bewertung nach Relevanz, Material-Ahnlichkeit, Usage
    v
[2] Evaluation: Ergebnisse gut genug?
    |
    |-- Nein: Verfeinerte Query -> zuruck zu Phase 2
    |         (neuer Agent-Schritt, max_steps begrenzt)
    |
    |-- Ja: Kontext bereit -> weiter zu Phase 4
```

### Interface 3.1: Internes Reranking

**Endpoint:** `POST /v1/rerank` (auch extern aufrufbar)

| Richtung | Feld            | Typ             | Beschreibung                                    |
| -------- | --------------- | --------------- | ----------------------------------------------- |
| Request  | `query`         | str             | Ursprungliche Anfrage                           |
| Request  | `chunks`        | list[ChunkInput]| `{ chunk_id, text, score, metadata }`           |
| Request  | `use_case`      | str             | Use-Case-Kennung                                |
| Request  | `config`        | RerankConfig    | `threshold` (0.3), `weights` (material: 0.4, param: 0.4, usage: 0.2) |
| Response | `reranked`      | list[ChunkInput]| Sortierte Chunks uber Threshold                 |
| Response | `threshold_passed` | bool         | Ob Mindest-Threshold erreicht wurde             |
| Response | `below_threshold` | list[ChunkInput]| Chunks unter Threshold                         |

### Interface 3.2: Interne Evaluation

**Endpoint:** `POST /v1/evaluate`

| Richtung | Feld          | Typ             | Beschreibung                            |
| -------- | ------------- | --------------- | --------------------------------------- |
| Request  | `query`       | str             | Ursprungliche Anfrage                   |
| Request  | `chunks`      | list[ChunkInput]| Gerankete Chunks                        |
| Request  | `use_case`    | str             | Use-Case-Kennung                        |
| Request  | `min_chunks`  | int             | Mindestanzahl relevanter Chunks (Default: 2) |
| Request  | `min_score`   | float           | Mindest-Score (Default: 0.5)            |
| Response | `result`      | bool            | Ob Qualitat ausreichend ist             |
| Response | `reasoning`   | str             | Begrundung der Entscheidung             |

### Agent-Aktionen

| Aktion            | Beschreibung                                       | Ziel-Service          |
| ----------------- | -------------------------------------------------- | --------------------- |
| `SEARCH`          | Semantische Suche in Collection                    | Embedding + VectorDB  |
| `SEARCH_CNC`      | Cross-Collection-Suche (nur GW St. Polten)        | Embedding + VectorDB  |
| `LOOKUP_SOURCES`  | Verfugbare Collections auflisten                   | VectorDB              |
| `RECALL_MEMORY`   | Sitzungskontext aus Memory lesen                   | Lokal (File/DB)       |
| `CLARIFY`         | Ruckfrage an den Benutzer formulieren              | Lokal (Response)      |
| `FINAL_ANSWER`    | Endgultige Antwort generieren                      | Lokal (LLM)           |

---

## Phase 4 -- Generierung & Ausgabe

```
Kontext (top_n Chunks + Metadaten)
    |
    v
[1] Evaluation Service: LLM generiert Antwort
    |   (Ollama, Modell: qwen2.5:14b)
    |
    v
[2] Evaluation Service: Quellenzuordnung (/v1/citations)
    |
    v
[3] Frontend Service: Anzeige von Antwort + Referenzen
    |
    v
[4] Frontend Service: Nutzerfeedback -> Evaluation Service (/v1/log)
```

### Interface 4.1: Quellenzuordnung

**Endpoint:** `POST /v1/citations`

| Richtung | Feld        | Typ               | Beschreibung                            |
| -------- | ----------- | ------------------ | --------------------------------------- |
| Request  | `answer`    | str                | Generierte Antwort                      |
| Request  | `chunks`    | list[CitationChunk]| `{ chunk_id, text, metadata }`          |
| Response | `citations` | list[dict]         | `{ sentence, sources: [chunk_id, ...] }`|

### Interface 4.2: Nutzerfeedback

**Endpoint:** `POST /v1/log`

| Richtung | Feld          | Typ   | Beschreibung                         |
| -------- | ------------- | ----- | ------------------------------------ |
| Request  | `query`       | str   | Ursprungliche Anfrage                |
| Request  | `answer`      | str   | Generierte Antwort                   |
| Request  | `feedback`    | str   | "positive" oder "negative"           |
| Request  | `session_id`  | str   | Sitzungs-ID                          |
| Request  | `use_case`    | str   | Use-Case-Kennung                     |

Feedback wird in **PostgreSQL** gespeichert und kann fur spateres Fine-Tuning verwendet werden.

---

## Admin-Interfaces

Der **Frontend Service** stellt Admin-Endpunkte bereit, die an den Evaluation Service weitergeleitet werden:

| Frontend-Route                      | Evaluation-Endpoint                | Beschreibung                       |
| ----------------------------------- | ---------------------------------- | ---------------------------------- |
| `GET /api/admin/documents`          | `GET /v1/admin/documents`          | Liste eingespeister Dokumente      |
| `GET /api/admin/queries`            | `GET /v1/admin/queries`            | Query-Historie                     |
| `GET /api/admin/memory/{use_case}`  | `GET /v1/admin/memory/{use_case}`  | Agent-Memory lesen                 |
| `PUT /api/admin/memory/{use_case}`  | `PUT /v1/admin/memory/{use_case}`  | Agent-Memory aktualisieren         |
| `GET /api/admin/prompts/{use_case}` | `GET /v1/admin/prompts/{use_case}` | System-Prompt lesen                |
| `PUT /api/admin/prompts/{use_case}` | `PUT /v1/admin/prompts/{use_case}` | System-Prompt aktualisieren        |

---

## Vollstandige Service-Abhangigkeitsmatrix

Diese Matrix zeigt, welcher Service welchen anderen Service aufruft:

| Aufrufer ↓ / Ziel →  | Cleaning | Data Structure | Embedding | VectorDB | Evaluation | Ollama  | Qdrant  | PostgreSQL |
| --------------------- | -------- | -------------- | --------- | -------- | ---------- | ------- | ------- | ---------- |
| **Frontend**          | x        | x              | x         | x        | x          |         |         |            |
| **Evaluation**        |          |                | x         | x        |            | x       |         | x          |
| **Embedding**         |          |                |           |          |            | x       |         |            |
| **VectorDB**          |          |                |           |          |            |         | x       |            |
| **Cleaning**          |          |                |           |          |            |         |         |            |
| **Data Structure**    |          |                |           |          |            |         |         |            |

---

## Konfiguration (Umgebungsvariablen)

| Variable                    | Service         | Default                          |
| --------------------------- | --------------- | -------------------------------- |
| `CLEANING_SERVICE_URL`      | Frontend        | `http://cleaning:8001`           |
| `DATA_STRUCTURE_SERVICE_URL`| Frontend        | `http://data_structure:8002`     |
| `EMBEDDING_SERVICE_URL`     | Frontend, Eval  | `http://embedding:8003`          |
| `VECTORDB_SERVICE_URL`      | Frontend, Eval  | `http://vectordb:8004`           |
| `EVALUATION_SERVICE_URL`    | Frontend        | `http://evaluation:8005`         |
| `OLLAMA_BASE_URL`           | Embedding, Eval | `http://ollama:11434`            |
| `EMBEDDING_MODEL`           | Embedding       | `qwen3-embedding:0.6b`          |
| `LLM_MODEL`                 | Evaluation      | `qwen2.5:14b`                   |
| `QDRANT_HOST`               | VectorDB        | `qdrant`                         |
| `QDRANT_PORT`               | VectorDB        | `6333`                           |
| `DATABASE_URL`              | Evaluation      | PostgreSQL Connection String     |
| `AGENT_MAX_STEPS`           | Evaluation      | `5`                              |

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
  sandbox/       – Isolierte Ausführung von Skill-Funktionen (Plugin-Layer)
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

## Authentifizierung & Login

Die gesamte App liegt hinter einem Login. Benutzer haben eine Rolle:

| Rolle | Rechte |
|---|---|
| `admin` | Voller Zugriff inkl. Dashboard (`/admin`): Use Cases + Benutzer verwalten |
| `user` | Nur Chat / Use-Case-Nutzung |

**Mechanik:** signiertes, zustandsloses Session-Cookie (HMAC-SHA256 über
`AUTH_SECRET`). Der SvelteKit-`hooks.server.ts` löst das Cookie auf und schützt
alle Routen außer `/login` und `/health`. `/admin/*` und `/api/admin/*` setzen
zusätzlich die Rolle `admin` voraus. Passwörter werden gehasht gespeichert
(PBKDF2-SHA256). Benutzer liegen in der `users`-Tabelle (Postgres).

### Erforderliche Env-Variablen

```bash
# Initialer Admin – wird beim Start angelegt, wenn die users-Tabelle leer ist
# (idempotent; nach dem ersten Start entfernbar). Weitere User via Dashboard.
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<sicheres-passwort>

# Secret zum Signieren der Session-Cookies (MUSS gesetzt sein)
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
AUTH_SECRET=<zufalls-secret>

# Master-Key für at-rest-Verschlüsselung der per Use Case hinterlegten API-Keys
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CONFIG_MASTER_KEY=<fernet-key>
```

`AUTH_SECRET` wird dem Frontend-Container, `ADMIN_*` dem evaluation-Service
übergeben (siehe `docker-compose.*.yml`; im Dev-Modus via `env_file: .env`).

### Erststart

1. `.env` ausfüllen (`ADMIN_USERNAME`, `ADMIN_PASSWORD`, `AUTH_SECRET`,
   `CONFIG_MASTER_KEY`).
2. DB frisch aufsetzen – `db/init.sql` enthält alle Tabellen inkl. `users`
   und `use_cases`.
3. Beim ersten Start wird der Admin aus der `.env` angelegt und die Use Cases
   aus `services/evaluation/config/use_cases.json` in die DB geseedet.
4. Login unter `/login`, Verwaltung unter `/admin`.

## Use Cases

| Kürzel | Beschreibung |
|---|---|
| `neumann` | Maschinenwartung – Störungsanalyse & Behebungsvorschläge |
| `gw_stpoelten` | CNC-Rüstungsdaten – Werkzeugalternativen |
| `wiener_linien` | Wissensassistent – Vorschriften & Prüfungsvorbereitung |

Use Cases werden in einer **JSON-Config** beschrieben, beim Start in die DB
**geseedet** und können danach im **Dashboard** bearbeitet und als JSON
**exportiert** werden.

### Konfiguration via JSON + Dashboard

- **Seed-Datei:** `services/evaluation/config/use_cases.json` (Pfad über
  `USE_CASES_CONFIG_PATH` überschreibbar) beschreibt pro Use Case Slug, Label,
  Beschreibung, Farben, Rollen, Agent-Actions, Default-Collection,
  Collection-Prefixes und System-Prompts.
- **Seeding beim Boot:** *create-if-missing* – bestehende DB-/Dashboard-Stände
  bleiben unberührt. Die JSON ist damit die Erst-Initialisierung, die DB der
  Live-Stand.
- **Dashboard** (`/admin/use-cases`, admin-only): Use Cases anlegen, bearbeiten,
  löschen und die aktuelle Konfiguration als `use_cases.json` exportieren.
  Änderungen wirken sofort (der evaluation-Service lädt die Registry neu).
- **Frontend:** Die Use-Case-Auswahl im Chat-UI wird dynamisch aus der DB
  geladen (`GET /v1/use-cases`) – kein Code-Eintrag mehr nötig.

- **Dokumente hochladen** funktioniert für jeden im Dashboard angelegten Use Case
  sofort: Der data_structure-Service schlägt Use Cases ohne eigenes Plugin in der
  DB nach und verarbeitet sie generisch; die Chunks landen in der
  **Default-Collection** des Use Case.
- **Die Suche liest alle Collections des Use Case** – alle, die mit einem der
  Collection-Prefixes beginnen (keine Prefixes → `<id>_`), nicht nur die
  Default-Collection. Nur wer im Request ausdrücklich eine `collection` angibt,
  grenzt auf diese eine ein. Andere Use Cases bleiben hart isoliert.
- **Default-Collection und Collection-Prefixes müssen zusammenpassen**, sonst
  würden Uploads außerhalb der Suche landen. Das Dashboard lehnt eine
  Default-Collection außerhalb der Prefixes ab; leer gelassen wird sie zu
  `<id>_default`.
- **Tabellen** (Reiter „Tabelle") landen in `<id>_default`, wenn das zu den
  Prefixes passt, sonst in `<prefix>tabellen` (z. B. `wl_tabellen`).

> Ein eigenes `services/data_structure/plugins/<id>.py` (mit `enrich_metadata()`)
> ist nur noch nötig, wenn ein Use Case eigene Ingestion-Metadaten-Logik braucht
> (wie GW St. Pölten mit seinen CNC-Daten) – das ist Code und nicht Teil der
> JSON-Config.

### Neuen Use Case anlegen (datei-basiert, optional)

> Weitgehend abgelöst durch JSON-Config + Dashboard (siehe oben). Das Script
> bleibt für den datei-basierten Workflow nutzbar.

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

## Skills mit ausführbaren Funktionen

Ein Skill ist zunächst Text, der an den Agent-System-Prompt gehängt wird. Er
kann zusätzlich **eine** Code-Datei mitbringen (`.py` **oder** `.js`), deren
Funktionen das LLM als `skillname.funktion(...)` in seiner Antwort aufruft. Ein
Parser erkennt diese Aufrufe, die Ausführung läuft in einem kurzlebigen,
isolierten Docker-Container, und der Aufruf wird im Antworttext durch das
Ergebnis ersetzt.

| | |
|---|---|
| Verwaltung | **Verwaltung → Skills → Bearbeiten → Ausführbare Funktionen** (nur Admin) |
| Freigabe | Funktionen mit `# ask for permission` fragen vor der Ausführung im Chat nach |
| Netzzugriff | Nur Hosts aus `# allow-network:`; ohne Angabe läuft der Container mit `--network none` |
| Variablen | Pro Skill ein JSON-Objekt (API-Keys etc.) — schreibbar nur für Admins, lesbar für niemanden |
| Beispiel | `wetter` (Open-Meteo), geseedet aus `services/evaluation/config/skills/` |
| Übersicht | Die Skill-Liste zeigt je Skill Sprache, Funktionen, Freigabepflicht, erreichbare Hosts und Variablenzahl |
| Export/Import | JSON pro Use Case — mit Code, **ohne** Variablenwerte |

**Erstmalige Einrichtung auf einer bestehenden Datenbank:** `make migrate`.
Der Plugin-Layer braucht Migration 0010. Datenbanken, die aus `db/init.sql`
entstanden sind, tragen keine Alembic-Marke — `make migrate` erkennt ihren
Stand, stempelt ihn und migriert von dort. Ohne die Migration bleibt der
Plugin-Layer inaktiv; bestehende Skills funktionieren unverändert weiter.

Das Runner-Image baut `make skill-runner` (in `make up` und `make dev`
enthalten). Master-Schalter und Limits stehen in `.env` unter
„Skill-Plugin-Layer"; **`SANDBOX_TOKEN` in Produktion setzen**.

- Skill schreiben: [`docs/skill_functions_developer.md`](docs/skill_functions_developer.md)
- Wie es intern funktioniert: [`docs/skill_functions_architecture.md`](docs/skill_functions_architecture.md)

## Use Case auf andere Instanz übertragen

**Verwaltung → Use Cases → Exportieren** packt einen kompletten Use Case in eine
ZIP-Datei: Konfiguration, Prompts, Skills, Dokumentliste, Collections samt
Vektoren, Originaldateien und Bilder, Chatverlauf, Feedback. API-Keys und
Werte von Skill-Variablen bleiben draußen.

**Use Case importieren** lädt die Datei auf einer anderen (oder derselben)
Instanz hoch. Existiert die ID dort schon, muss ein neuer Name vergeben werden —
Collections, Präfix und Dateipfade werden dann mit umbenannt. Vektoren werden
1:1 kopiert, daher muss das Ziel dasselbe Embedding-Modell nutzen. Braucht
Migration 0012 (`make migrate`).

→ **Details: [`docs/use_case_transfer.md`](docs/use_case_transfer.md)**

## Pipeline per API nutzen

Die Pipeline ist nicht nur über die Chat-Oberfläche erreichbar, sondern auch per
HTTP-API. Bereitgestellt wird sie vom **Frontend** — dem einzigen von außen
erreichbaren Dienst — und intern an den evaluation-Service durchgeschleust.

Weil ein Agentenlauf Sekunden bis Minuten dauert, ist die API asynchron:

```
POST /api/v1/chat                     → 202  { request_id, status: "queued" }
GET  /api/v1/chat/{request_id}        →      { status: queued|running|done|error }
GET  /api/v1/chat/{request_id}/result → 200  Antwort  |  409 solange unfertig
GET  /api/v1/sessions/{session_id}    →      kompletter Chat als JSON
```

Gesichert mit `Authorization: Bearer <token>`. Tokens werden im Dashboard unter
**Verwaltung → API-Tokens** angelegt (einmalig sichtbar, gespeichert wird nur der
Hash) und lassen sich auf einzelne Use Cases einschränken, befristen und sperren.
Bilder werden als base64 im JSON mitgeschickt.

Jobs liegen in Postgres (`api_jobs`) und überleben einen Neustart; ein
Hintergrund-Worker im evaluation-Service arbeitet sie mit begrenzter
Parallelität ab, damit API-Last den interaktiven Chat nicht ausbremst.

Die **Endpunkt-Referenz wird aus dem Code erzeugt** und ist in der laufenden
Instanz im Dashboard unter **Verwaltung → API-Dokumentation** zu finden —
inklusive Feldtabellen, Statuscodes und fertiger curl-Aufrufe. Maschinenlesbar
liegt dieselbe Beschreibung unter `/api/v1/openapi.json`.

→ **Ablauf, Einrichtung und Betrieb: [`docs/public_api.md`](docs/public_api.md)**

## Bilder im Prompt (Vision)

Im Chat können pro Anfrage Bilder angehängt werden (📎-Button, max. 6). Sie
werden **im selben Prompt** an das Chat-Modell übergeben und dort mitverarbeitet
– kein separater Vision-Schritt.

- **Voraussetzung:** `LLM_MODEL` (bzw. der Use-Case-Override) muss ein
  **vision-fähiges** Modell sein. Beispiele:
  - Ollama: `llava`, `llama3.2-vision`
  - OpenAI-kompatibel (inkl. Mistral-API): `pixtral-*`

  Ein reines Textmodell (z. B. `ministral`, `qwen2.5`) ignoriert angehängte
  Bilder.
- **Transport:** Bilder werden als Data-URIs gesendet. Die Provider normalisieren
  selbst – Ollama erhält rohes base64 im `images`-Feld der Nachricht, OpenAI ein
  `content`-Array mit `image_url`-Parts (`shared/llm/images.py`).
- **Multi-Agent:** Bei aufgeteilten Anfragen erhalten alle Sub-Agenten die Bilder.
- **Verlauf:** Angehängte Bilder werden mit der Anfrage gespeichert und beim
  „Laden" einer Unterhaltung wiederhergestellt.

> Hinweis: Persistierte Bilder vergrößern die `queries`-Zeilen (base64 in JSONB).
> Bei intensiver Nutzung ggf. auf Objekt-Speicher auslagern.

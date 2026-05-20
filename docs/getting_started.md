# Getting Started – Projekt starten

Diese Anleitung beschreibt, wie die RAG Platform in zwei Modi gestartet wird:

1. **Developer Mode** – Alles auf einer Maschine mit Hot-Reload
2. **Produktion** – Drei-Server-Setup fuer Staging/Production

---

## Architektur-Ueberblick

```
┌───────────────────── Server A (Core) ──────────────────────┐
│  Cleaning (8001)  Data Structure (8002)  Embedding (8003)  │
│  VectorDB (8004)  Qdrant (6333)                            │
└────────────────────────────────────────────────────────────┘
         ▲                                     ▲
         │                                     │
┌────────┴──── Server B (Inference) ───────────┘─────────────┐
│  Ollama (11434)   Evaluation/Agent (8005)                  │
└──────────────────────┬─────────────────────────────────────┘
                       │
                       ▼
┌───────────────── Server C (Frontend) ──────────────────────┐
│  PostgreSQL (5432)   Frontend/Node (3000)                  │
└────────────────────────────────────────────────────────────┘
```

Im Developer Mode laufen alle Teile als Docker-Container auf einer
Maschine. In Produktion werden sie auf drei Server verteilt.

---

## Voraussetzungen

| Anforderung | Developer | Produktion |
|-------------|-----------|------------|
| Docker | >= 24.0 | >= 24.0 |
| Docker Compose | v2 | v2 |
| RAM | 16 GB mit Ollama / 4 GB ohne (dev-up-light) | 16 GB auf Server B |
| GPU | Optional, beschleunigt LLM | Empfohlen auf Server B |
| Node.js | 20+ (nur fuer Frontend-Dev ohne Docker) | – |
| Python | 3.11+ (nur fuer Tests ohne Docker) | – |
| Make | Optional, fuer Makefile-Targets | – |

---

# Developer Mode

## Schnellstart (3 Befehle)

```bash
# 1. Repository klonen
git clone <repo-url> && cd rag-platform

# 2. Env-Datei erstellen
cp .env.example .env

# 3. Alles starten (inkl. lokales Ollama)
make dev-up
```

Das ist alles. Docker baut alle Images und startet alle Services.

### Leichtgewichtiger Start (ohne lokales Ollama)

Wenn Ollama und/oder MinerU auf einem externen Server laufen (z.B. ein
GPU-Server im Netzwerk), koennen die schweren Container uebersprungen
werden:

```bash
# 1. Env-Datei erstellen
cp .env.example .env

# 2. Externe URLs in .env setzen
echo 'OLLAMA_BASE_URL=http://gpu-server.local:11434' >> .env

# Optional: MinerU fuer PDF-Parsing
echo 'USE_MINERU=true' >> .env
echo 'MINERU_API_URL=http://gpu-server.local:8000' >> .env

# 3. Ohne lokales Ollama starten
make dev-up-light
```

So braucht die lokale Maschine nur ~4 GB RAM statt 16 GB.

---

## Schritt fuer Schritt

### 1. Env-Datei anlegen

```bash
cp .env.example .env
```

Die Defaults in `.env.example` sind fuer den lokalen Betrieb vorkonfiguriert
(Docker-Service-Namen als Hostnamen). Normalerweise muss nichts geaendert
werden.

**Optionale Anpassungen in `.env`:**

| Variable | Default | Wann aendern? |
|----------|---------|---------------|
| `POSTGRES_PASSWORD` | `changeme` | Immer empfohlen |
| `LOG_LEVEL` | `INFO` | Auf `DEBUG` fuer detaillierte Logs |
| `LLM_MODEL` | `qwen2.5:14b` | Kleineres Modell fuer schwache Hardware |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Anderes Embedding-Modell |
| `USE_MINERU` | `false` | `true` wenn MinerU verfuegbar |

### 2. Services starten

**Variante A – Mit lokalem Ollama** (~16 GB RAM noetig):

```bash
make dev-up
```

**Variante B – Ohne lokales Ollama** (~4 GB RAM, Ollama laeuft extern):

```bash
# Externe Ollama-URL in .env setzen
echo 'OLLAMA_BASE_URL=http://gpu-server.local:11434' >> .env

# Optional: Externen MinerU-Server nutzen
echo 'USE_MINERU=true' >> .env
echo 'MINERU_API_URL=http://gpu-server.local:8000' >> .env

make dev-up-light
```

**Was passiert:**
- PostgreSQL und Qdrant starten als Infrastruktur-Container.
- Ollama startet nur bei `make dev-up` (Profil `local-ollama`).
- Alle 5 Python-Services starten mit `uvicorn --reload` (Hot-Reload).
- Das Frontend startet als Node.js-Container.
- Die `init.sql` erstellt automatisch alle Datenbanktabellen.

### 3. Ollama-Modelle herunterladen

Beim ersten Start muessen die LLM-Modelle geladen werden.

**Lokales Ollama** (nach `make dev-up`):

```bash
make pull-models

# Oder manuell:
docker exec ollama ollama pull qwen2.5:14b          # LLM (~9 GB)
docker exec ollama ollama pull qwen3-embedding:0.6b  # Embedding (~250 MB)
```

**Externes Ollama** (nach `make dev-up-light`):
Die Modelle muessen direkt auf dem externen Server geladen werden.

**Tipp:** Fuer schwache Hardware ein kleineres LLM verwenden:

```bash
docker exec ollama ollama pull qwen2.5:7b
# In .env: LLM_MODEL=qwen2.5:7b
```

### 4. Pruefen, ob alles laeuft

```bash
# Alle Container pruefen
docker compose -f docker-compose.dev.yml ps

# Health-Checks
curl http://localhost:8001/health   # Cleaning
curl http://localhost:8002/health   # Data Structure
curl http://localhost:8003/health   # Embedding
curl http://localhost:8004/health   # VectorDB
curl http://localhost:8005/health   # Evaluation
curl http://localhost:3000/health   # Frontend
curl http://localhost:6333/healthz  # Qdrant
curl http://localhost:11434/api/tags # Ollama (zeigt installierte Modelle)
```

Alle sollten `{"status": "ok", ...}` zurueckgeben.

### 5. Frontend oeffnen

Im Browser: **http://localhost:3000**

---

## Entwickler-Workflow

### Hot-Reload

Alle Python-Services mounten ihre Quellverzeichnisse als Docker-Volumes
und laufen mit `uvicorn --reload`. Aenderungen an `.py`-Dateien werden
automatisch erkannt – der Service startet innerhalb von 1-2 Sekunden neu.

Betroffene Volumes:

| Service | Gemountetes Verzeichnis |
|---------|------------------------|
| cleaning | `./services/cleaning:/app` |
| data_structure | `./services/data_structure:/app` |
| embedding | `./services/embedding:/app` |
| vectordb | `./services/vectordb:/app` |
| evaluation | `./services/evaluation:/app` |
| Alle Python-Services | `./shared:/shared` |

**Shared Package:** Aenderungen am `shared/`-Package werden ebenfalls
live erkannt, da es als editierbares Package installiert ist (`pip install -e`).

### Frontend-Entwicklung

Das Frontend im Docker-Container hat **kein** Hot-Reload fuer Svelte. Fuer
aktive Frontend-Entwicklung den Dev-Server lokal starten:

```bash
cd services/frontend
npm install
npm run dev
```

Der Vite-Dev-Server laeuft auf `http://localhost:5173` mit Hot-Module-
Replacement. Die Backend-Services laufen weiter in Docker.

Env-Variablen fuer den lokalen Frontend-Dev-Server in
`services/frontend/.env` anlegen:

```
EVALUATION_SERVICE_URL=http://localhost:8005
CLEANING_SERVICE_URL=http://localhost:8001
DATA_STRUCTURE_SERVICE_URL=http://localhost:8002
EMBEDDING_SERVICE_URL=http://localhost:8003
VECTORDB_SERVICE_URL=http://localhost:8004
```

### Logs anzeigen

```bash
# Alle Services
make dev-logs

# Einzelner Service
docker compose -f docker-compose.dev.yml logs -f evaluation

# Logs sind strukturiertes JSON (eine Zeile pro Eintrag):
# {"timestamp": "...", "level": "INFO", "service": "evaluation-service",
#  "request_id": "...", "message": "Request finished: POST /v1/agent/query 200",
#  "extra": {"duration_ms": 1234}}
```

### Tests ausfuehren

```bash
# Alle Unit-Tests (shared + 5 Python-Services + Frontend-Check)
make test

# Einzelner Service
cd services/cleaning && python -m pytest tests/ -v

# Shared Package
cd shared && python -m pytest -v

# Frontend Type-Check
cd services/frontend && npm run check

# Linting
make lint
```

### Integration-Tests

Die Integration-Tests laufen gegen die laufenden Services:

```bash
make integration-test
```

Dies startet einen temporaeren Container, der alle Endpunkte end-to-end
testet (Ingestion-Pipeline + Agent-Queries fuer alle Use Cases).

### Datenbank zuruecksetzen

```bash
# Alle Daten loeschen und Tabellen neu erstellen
docker compose -f docker-compose.dev.yml down -v   # Loescht alle Volumes!
make dev-up                                          # Startet frisch
```

**Achtung:** `-v` loescht alle Volumes (PostgreSQL, Qdrant, Ollama-Modelle).
Ollama-Modelle muessen danach erneut heruntergeladen werden.

Nur die Datenbank zuruecksetzen (ohne Qdrant/Ollama zu verlieren):

```bash
docker compose -f docker-compose.dev.yml stop postgres
docker volume rm $(docker volume ls -q | grep pgdata)
docker compose -f docker-compose.dev.yml up -d postgres
```

### Alembic-Migrationen (Datenbankschema)

```bash
cd db
pip install -r requirements.txt

# Aktuelle Migration anwenden
alembic -c alembic.ini upgrade head

# Neue Migration erstellen (nach Schema-Aenderung in db/models.py)
alembic -c alembic.ini revision --autogenerate -m "Beschreibung"
```

### Services stoppen

```bash
make dev-down

# Oder:
docker compose -f docker-compose.dev.yml down
```

---

# Produktion

## Uebersicht

In Produktion wird die Plattform auf drei Server verteilt:

| Server | Compose-Datei | Services | Min. RAM |
|--------|---------------|----------|----------|
| **A** (Core) | `docker-compose.core.yml` | Cleaning, Data Structure, Embedding, VectorDB, Qdrant | 4 GB |
| **B** (Inference) | `docker-compose.inference.yml` | Ollama, Evaluation/Agent | 16 GB |
| **C** (Frontend) | `docker-compose.frontend.yml` | PostgreSQL, Frontend (Node) | 2 GB |

**Warum drei Server?**
- Server B braucht viel RAM/GPU fuer das LLM.
- Server A macht I/O-intensive Arbeit (Embedding, Vektor-Suche).
- Server C ist leichtgewichtig (Frontend + DB).

Die Server muessen sich gegenseitig per IP/Hostname erreichen koennen
auf den konfigurierten Ports.

---

## Schritt 1: Server A starten (Core Pipeline)

```bash
cd /opt/rag-platform   # Installations-Verzeichnis

# Env-Datei aus Vorlage erstellen
cp .env.example .env
```

In `.env` anpassen:

```bash
# PFLICHT: Ollama laeuft auf Server B
OLLAMA_BASE_URL=http://<IP_SERVER_B>:11434

# Optional: Anpassen
LOG_LEVEL=INFO
DEFAULT_CHUNK_SIZE=256   # in Tokens (SentenceSplitter), ≈ 1000 Zeichen DE
```

Starten:

```bash
docker compose -f docker-compose.core.yml up -d
```

Pruefen:

```bash
docker compose -f docker-compose.core.yml ps

curl http://localhost:8001/health   # Cleaning
curl http://localhost:8002/health   # Data Structure
curl http://localhost:8003/health   # Embedding
curl http://localhost:8004/health   # VectorDB
curl http://localhost:6333/healthz  # Qdrant
```

---

## Schritt 2: Server B starten (Inference)

```bash
cp .env.example .env
```

In `.env` anpassen:

```bash
# PFLICHT: Embedding und VectorDB laufen auf Server A
EMBEDDING_SERVICE_URL=http://<IP_SERVER_A>:8003
VECTORDB_SERVICE_URL=http://<IP_SERVER_A>:8004

# PFLICHT: Datenbank laeuft auf Server C
DATABASE_URL=postgresql://rag:<PASSWORT>@<IP_SERVER_C>:5432/ragplatform

# Optional
AGENT_MAX_STEPS=5
MEMORY_MAX_CHARS=4000
LLM_MODEL=qwen2.5:14b
```

Starten:

```bash
docker compose -f docker-compose.inference.yml up -d
```

Modelle herunterladen (nur beim ersten Start):

```bash
docker exec ollama ollama pull qwen2.5:14b
docker exec ollama ollama pull qwen3-embedding:0.6b
```

Pruefen:

```bash
docker compose -f docker-compose.inference.yml ps

curl http://localhost:8005/health       # Evaluation
curl http://localhost:11434/api/tags    # Ollama – zeigt installierte Modelle
```

---

## Schritt 3: Server C starten (Frontend + Datenbank)

```bash
cp .env.example .env
```

In `.env` anpassen:

```bash
# PFLICHT: Sicheres Passwort setzen
POSTGRES_PASSWORD=<sicheres-passwort>

# PFLICHT: Alle Backend-Service-URLs
EVALUATION_SERVICE_URL=http://<IP_SERVER_B>:8005
CLEANING_SERVICE_URL=http://<IP_SERVER_A>:8001
DATA_STRUCTURE_SERVICE_URL=http://<IP_SERVER_A>:8002
EMBEDDING_SERVICE_URL=http://<IP_SERVER_A>:8003
VECTORDB_SERVICE_URL=http://<IP_SERVER_A>:8004
```

Starten:

```bash
docker compose -f docker-compose.frontend.yml up -d
```

Pruefen:

```bash
docker compose -f docker-compose.frontend.yml ps

curl http://localhost:3000/health   # Frontend

# Datenbank-Tabellen pruefen
docker exec -it $(docker ps -qf name=postgres) \
  psql -U rag -d ragplatform -c "\dt"
```

Erwartete Tabellen: `queries`, `feedback`, `ingestion_log`, `agent_memory`,
`evaluation_runs`.

---

## Schritt 4: Gesamtsystem pruefen

Von einem beliebigen Rechner aus, der alle Server erreichen kann:

```bash
# Alle Health-Checks auf einen Blick
for url in \
  "http://<IP_A>:8001/health" \
  "http://<IP_A>:8002/health" \
  "http://<IP_A>:8003/health" \
  "http://<IP_A>:8004/health" \
  "http://<IP_A>:6333/healthz" \
  "http://<IP_B>:8005/health" \
  "http://<IP_B>:11434/api/tags" \
  "http://<IP_C>:3000/health"; do
  echo -n "$url → "
  curl -sf "$url" | head -c 80
  echo
done
```

Frontend oeffnen: **http://\<IP_SERVER_C\>:3000**

---

## Env-Variablen-Referenz (komplett)

### Pflicht-Variablen pro Server

| Variable | Server A | Server B | Server C |
|----------|:--------:|:--------:|:--------:|
| `OLLAMA_BASE_URL` | Ja | – | – |
| `EMBEDDING_SERVICE_URL` | – | Ja | Ja |
| `VECTORDB_SERVICE_URL` | – | Ja | Ja |
| `DATABASE_URL` | – | Ja | – |
| `POSTGRES_PASSWORD` | – | – | Ja |
| `EVALUATION_SERVICE_URL` | – | – | Ja |
| `CLEANING_SERVICE_URL` | – | – | Ja |
| `DATA_STRUCTURE_SERVICE_URL` | – | – | Ja |

### Optionale Variablen (alle Server)

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `LLM_MODEL` | `qwen2.5:14b` | Ollama-Modell fuer Textgenerierung |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Ollama-Modell fuer Embeddings |
| `EMBEDDING_DIMENSION` | `768` | Vektordimension (muss zum Modell passen) |
| `EMBED_BATCH_SIZE` | `50` | Max. Chunks pro Embed-Batch |
| `DEFAULT_CHUNK_SIZE` | `256` | Chunk-Groesse in **Tokens** (SentenceSplitter; ≈ 1000 Zeichen DE) |
| `DEFAULT_CHUNK_OVERLAP` | `32` | Ueberlappung in **Tokens** (≈ 130 Zeichen DE) |
| `AGENT_MAX_STEPS` | `5` | Max. Schritte im ReAct-Loop |
| `MEMORY_MAX_CHARS` | `4000` | Max. Zeichen im Session-Gedaechtnis |
| `USE_MINERU` | `false` | MinerU fuer PDF-Parsing verwenden |
| `MINERU_API_URL` | `http://mineru:8000` | MinerU-Service-URL |
| `QDRANT_HOST` | `qdrant` | Qdrant-Hostname |
| `QDRANT_PORT` | `6333` | Qdrant-Port |
| `POSTGRES_DB` | `ragplatform` | Datenbankname |
| `POSTGRES_USER` | `rag` | Datenbank-Benutzer |
| `POSTGRES_PORT` | `5432` | PostgreSQL-Port |

---

## Persistente Daten (Volumes)

| Volume | Server | Compose-Datei | Inhalt |
|--------|--------|---------------|--------|
| `qdrant_data` | A | core.yml | Vektordaten (alle Collections) |
| `ollama_data` | B | inference.yml | Heruntergeladene LLM-Modelle |
| `pgdata` | C | frontend.yml | PostgreSQL-Datenbank |

### Backup

```bash
# PostgreSQL – auf Server C
docker exec $(docker ps -qf name=postgres) \
  pg_dump -U rag ragplatform > backup_$(date +%Y%m%d).sql

# Qdrant – auf Server A (pro Collection)
curl -X POST http://localhost:6333/collections/neumann_machines/snapshots

# PostgreSQL wiederherstellen
cat backup_20260317.sql | docker exec -i $(docker ps -qf name=postgres) \
  psql -U rag ragplatform
```

---

## Update / Neustart eines Service

```bash
# Einzelnen Service neu bauen und starten (z.B. nach Code-Aenderung)
docker compose -f docker-compose.core.yml build cleaning
docker compose -f docker-compose.core.yml up -d cleaning

# Alle Services eines Servers neu bauen
docker compose -f docker-compose.core.yml up --build -d

# Nur neustarten (ohne Rebuild)
docker compose -f docker-compose.core.yml restart cleaning
```

---

## Troubleshooting

### Embedding-Service startet nicht

Der Embedding-Service braucht Zugriff auf Ollama (Server B):

```bash
# Von Server A aus testen:
docker exec $(docker ps -qf name=embedding) \
  curl -sf $OLLAMA_BASE_URL/api/tags
```

Falls nicht erreichbar: Firewall-Regel fuer Port 11434 pruefen.

### Evaluation-Service gibt 503

Der Evaluation-Service braucht Zugriff auf Embedding (Server A) und
VectorDB (Server A):

```bash
docker exec $(docker ps -qf name=evaluation) \
  curl -sf $EMBEDDING_SERVICE_URL/health

docker exec $(docker ps -qf name=evaluation) \
  curl -sf $VECTORDB_SERVICE_URL/health
```

### Frontend zeigt Fehler / leere Seite

Pruefen, ob alle Service-URLs in der `.env` korrekt sind:

```bash
docker exec $(docker ps -qf name=frontend) env | grep SERVICE_URL
```

### Datenbank-Tabellen fehlen

Die `init.sql` wird nur beim ersten Start ausgefuehrt. Falls die Tabellen
fehlen:

```bash
# Option 1: init.sql manuell ausfuehren
docker exec -i $(docker ps -qf name=postgres) \
  psql -U rag ragplatform < db/init.sql

# Option 2: Alembic-Migration
cd db
pip install -r requirements.txt
alembic -c alembic.ini upgrade head
```

### Ollama-Modelle fehlen

```bash
# Installierte Modelle pruefen
curl http://<IP_SERVER_B>:11434/api/tags

# Modelle herunterladen
docker exec ollama ollama pull qwen2.5:14b
docker exec ollama ollama pull qwen3-embedding:0.6b
```

### Container-Logs pruefen

```bash
# Letzten 100 Zeilen eines Service
docker compose -f docker-compose.core.yml logs --tail 100 cleaning

# Live-Logs
docker compose -f docker-compose.core.yml logs -f embedding
```

### Qdrant-Daten pruefen

```bash
# Alle Collections auflisten
curl http://localhost:6333/collections

# Punkte in einer Collection zaehlen
curl http://localhost:6333/collections/neumann_machines
```

---

## Makefile-Targets (Developer Mode)

| Target | Beschreibung |
|--------|--------------|
| `make dev-up` | Alle Services starten (inkl. lokales Ollama, ~16 GB RAM) |
| `make dev-up-light` | Ohne Ollama starten (externe URL in `.env`, ~4 GB RAM) |
| `make dev-down` | Alle Services stoppen |
| `make dev-logs` | Live-Logs aller Services |
| `make pull-models` | Ollama-Modelle herunterladen (LLM + Embedding) |
| `make test` | Unit-Tests (shared + alle Python-Services + Frontend) |
| `make lint` | Ruff + Mypy ueber die gesamte Codebase |
| `make integration-test` | E2E-Tests gegen laufende Services |
| `make help` | Alle Targets mit Beschreibung anzeigen |

---

## Compose-Dateien im Ueberblick

| Datei | Zweck | Env-Datei |
|-------|-------|-----------|
| `docker-compose.dev.yml` | Lokale Entwicklung (alle Services, Hot-Reload) | `.env` |
| `docker-compose.core.yml` | Produktion Server A (Core Pipeline) | `.env` |
| `docker-compose.inference.yml` | Produktion Server B (LLM + Agent) | `.env` |
| `docker-compose.frontend.yml` | Produktion Server C (Frontend + DB) | `.env` |

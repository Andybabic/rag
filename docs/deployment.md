# Deployment – Drei-Server-Setup

Die RAG Platform laeuft auf drei Servern. Jeder Server hat eine eigene
Docker-Compose-Datei und ein eigenes `.env`.

```
Server A (Core)       Server B (Inference)     Server C (Frontend)
┌──────────────┐      ┌──────────────────┐     ┌──────────────────┐
│ Cleaning     │      │ Ollama           │     │ PostgreSQL       │
│ Data Struct. │◄─────│ Evaluation/Agent │────►│ Frontend (Node)  │
│ Embedding    │      └──────────────────┘     └──────────────────┘
│ VectorDB     │
│ Qdrant       │
└──────────────┘
```

## Voraussetzungen

- Docker >= 24.0 und Docker Compose v2
- Mindestens 16 GB RAM auf Server B (Ollama + LLM)
- Die Server muessen sich gegenseitig per IP/Hostname erreichen koennen
- Ports: 8001-8005 (Server A/B), 5432 (Server C), 3000 (Server C)

---

## 1. Server A starten (Core Pipeline)

Server A fuehrt die Datenverarbeitung aus: Cleaning, Chunking, Embedding
und Vektorspeicherung.

```bash
cd /opt/rag-platform   # oder Ihr Installations-Verzeichnis

# .env anlegen und OLLAMA_BASE_URL auf Server B setzen
cp .env.example .env

# In .env anpassen:
#   OLLAMA_BASE_URL=http://<IP_SERVER_B>:11434

docker compose -f docker-compose.core.yml up -d
```

**Pruefen:**

```bash
# Alle Container gesund?
docker compose -f docker-compose.core.yml ps

# Health-Checks
curl http://localhost:8001/health   # Cleaning
curl http://localhost:8002/health   # Data Structure
curl http://localhost:8003/health   # Embedding
curl http://localhost:8004/health   # VectorDB
```

---

## 2. Server B starten (Inference)

Server B fuehrt die LLM-Inferenz aus: Ollama fuer Sprachmodell und der
Evaluation-Service mit dem ReAct-Agent.

```bash
cp .env.example .env

# In .env anpassen:
#   EMBEDDING_SERVICE_URL=http://<IP_SERVER_A>:8003
#   VECTORDB_SERVICE_URL=http://<IP_SERVER_A>:8004
#   DATABASE_URL=postgresql://rag:<PASSWORT>@<IP_SERVER_C>:5432/ragplatform

docker compose -f docker-compose.inference.yml up -d
```

**Modelle herunterladen:**

```bash
# LLM-Modell (Textgenerierung)
docker exec ollama ollama pull qwen2.5:14b

# Embedding-Modell (Vektoren)
docker exec ollama ollama pull qwen3-embedding:0.6b
```

**Pruefen:**

```bash
docker compose -f docker-compose.inference.yml ps
curl http://localhost:8005/health   # Evaluation
curl http://localhost:11434/api/tags # Ollama – installierte Modelle
```

---

## 3. Server C starten (Frontend + Datenbank)

Server C betreibt die Datenbank und das Web-Frontend.

```bash
cp .env.example .env

# In .env anpassen:
#   POSTGRES_PASSWORD=<sicheres-passwort>
#   EVALUATION_SERVICE_URL=http://<IP_SERVER_B>:8005
#   CLEANING_SERVICE_URL=http://<IP_SERVER_A>:8001
#   DATA_STRUCTURE_SERVICE_URL=http://<IP_SERVER_A>:8002
#   EMBEDDING_SERVICE_URL=http://<IP_SERVER_A>:8003
#   VECTORDB_SERVICE_URL=http://<IP_SERVER_A>:8004

docker compose -f docker-compose.frontend.yml up -d
```

**Pruefen:**

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

## Health-Check-Uebersicht

| Service        | URL                          | Compose-Datei          |
|----------------|------------------------------|------------------------|
| Cleaning       | `http://server-a:8001/health`| core.yml               |
| Data Structure | `http://server-a:8002/health`| core.yml               |
| Embedding      | `http://server-a:8003/health`| core.yml               |
| VectorDB       | `http://server-a:8004/health`| core.yml               |
| Qdrant         | `http://server-a:6333/healthz`| core.yml              |
| Ollama         | `http://server-b:11434/api/tags`| inference.yml       |
| Evaluation     | `http://server-b:8005/health`| inference.yml          |
| PostgreSQL     | `pg_isready -U rag`         | frontend.yml           |
| Frontend       | `http://server-c:3000/health`| frontend.yml           |

---

## Lokale Entwicklung (ein Server)

Fuer lokale Entwicklung alle Services auf einer Maschine:

```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up --build -d

# Modelle laden
docker exec ollama ollama pull qwen2.5:14b
docker exec ollama ollama pull qwen3-embedding:0.6b
```

Die `docker-compose.dev.yml` startet alle Services mit Hot-Reload und
gemounten Volumes.

---

## Integration-Tests

```bash
# Setzt voraus, dass alle Services laufen (lokal oder remote)
docker compose -f docker-compose.dev.yml --profile test run --rm integration-test
```

---

## Volumes und Persistenz

| Volume       | Compose-Datei   | Inhalt                          |
|-------------|-----------------|----------------------------------|
| `qdrant_data`| core.yml       | Qdrant-Vektordaten               |
| `ollama_data`| inference.yml  | Heruntergeladene Ollama-Modelle  |
| `pgdata`     | frontend.yml   | PostgreSQL-Datenbank             |

**Backup:**

```bash
# PostgreSQL
docker exec $(docker ps -qf name=postgres) \
  pg_dump -U rag ragplatform > backup_$(date +%Y%m%d).sql

# Qdrant – Snapshot erstellen
curl -X POST http://server-a:6333/collections/neumann_machines/snapshots
```

---

## Troubleshooting

**Embedding-Service startet nicht:**
Pruefen Sie, ob `OLLAMA_BASE_URL` von Server A aus erreichbar ist:
```bash
docker exec $(docker ps -qf name=embedding) curl -sf $OLLAMA_BASE_URL/api/tags
```

**Evaluation-Service 503:**
Der Evaluation-Service braucht Zugriff auf Embedding-Service (Server A)
und VectorDB (Server A). Pruefen Sie die Netzwerk-Konnektivitaet.

**Frontend zeigt Fehler:**
Pruefen Sie, ob alle Service-URLs in der `.env` korrekt sind:
```bash
docker exec $(docker ps -qf name=frontend) env | grep SERVICE_URL
```

**Datenbank-Migration:**
```bash
cd db
pip install -r requirements.txt
alembic -c alembic.ini upgrade head
```

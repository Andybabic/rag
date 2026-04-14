# Architektur

Die Plattform besteht aus sechs Services die als Docker-Container laufen und über HTTP kommunizieren.

## Services

1. **Cleaning Service** – Rohdokumente → strukturiertes Markdown (MineU für PDFs)
2. **Data Structure Service** – Markdown → Chunks mit Metadaten (Plugin-basiert)
3. **Embedding Service** – Text → Vektoren via Ollama
4. **Vector DB Service** – Qdrant: Speicherung & Ähnlichkeitssuche
5. **Evaluation & Agent Service** – ReAct-Agent, Reranking, Citations
6. **Frontend** – Chat-Interface mit Upload & Quellenanzeige

## Shared Package

Alle Services verwenden das `shared`-Package für einheitliche Modelle, Fehlerformate und Request-Tracing.

## Kommunikation

Services kommunizieren über HTTP. `X-Request-ID` wird über die `RequestIDMiddleware` durchgereicht.

## Konfiguration

Alle Service-Adressen werden über Umgebungsvariablen konfiguriert (siehe `.env.example`).

COMPOSE = docker compose -f docker-compose.dev.yml --env-file .env.dev
PY_SERVICES = cleaning data_structure embedding vectordb evaluation

.PHONY: frontend-build up-dev-full dev dev-fe frontend-dev down logs test lint integration-test pull-models doc reset help

# Backend service ports published to localhost (see docker-compose.dev.yml).
FE_DEV_ENV = \
	EVALUATION_SERVICE_URL=http://localhost:8005 \
	CLEANING_SERVICE_URL=http://localhost:8001 \
	DATA_STRUCTURE_SERVICE_URL=http://localhost:8002 \
	EMBEDDING_SERVICE_URL=http://localhost:8003 \
	VECTORDB_SERVICE_URL=http://localhost:8004

# ── Docker Compose ───────────────────────────────────────────

frontend-build: ## Frontend bauen (SvelteKit)
	@echo "Baue Frontend ..."
	cd services/frontend && npm install && npm run build
	@echo "Frontend-Build fertig."

up-dev-full: frontend-build ## Start all services (inkl. lokales Ollama)
	@test -f .env.dev || cp .env.example .env.dev
	$(COMPOSE) --profile local-ollama up --build -d
	@echo ""
	@echo "Alle Services gestartet (inkl. lokalem Ollama)."
	@echo "Ollama-Modelle laden:  make pull-models"
	@echo "Logs anzeigen:         make dev-logs"

dev: frontend-build ## Start ohne Ollama (externe URL in .env.dev setzen)
	@test -f .env.dev || cp .env.example .env.dev
	@if ! grep -q '^OLLAMA_BASE_URL=' .env.dev 2>/dev/null; then \
		echo ""; \
		echo "FEHLER: OLLAMA_BASE_URL ist nicht in .env.dev gesetzt."; \
		echo ""; \
		echo "Setzen Sie die URL des externen Ollama-Servers:"; \
		echo "  echo 'OLLAMA_BASE_URL=http://<HOST>:11434' >> .env.dev"; \
		echo ""; \
		echo "Oder starten Sie mit lokalem Ollama:"; \
		echo "  make up-dev-full"; \
		echo ""; \
		exit 1; \
	fi
	$(COMPOSE) up --build -d
	@echo ""
	@echo "Services gestartet (ohne lokales Ollama)."
	@echo "Ollama-URL: $$(grep '^OLLAMA_BASE_URL=' .env.dev)"

dev-fe: ## Backend in Docker, Frontend mit Vite-Hot-Reload auf Host (localhost:5173)
	@test -f .env.dev || cp .env.example .env.dev
	@if ! grep -q '^OLLAMA_BASE_URL=' .env.dev 2>/dev/null; then \
		echo "FEHLER: OLLAMA_BASE_URL fehlt in .env.dev – siehe 'make dev'."; exit 1; \
	fi
	@echo "Starte Backend-Services (ohne Frontend-Container) ..."
	$(COMPOSE) up --build -d \
		postgres qdrant cleaning data_structure embedding vectordb evaluation
	@cd services/frontend && [ -d node_modules ] || npm install
	@echo ""
	@echo "Frontend dev server mit Hot-Reload → http://localhost:5173"
	@echo "Backend-Logs:  make logs   |   Stop:  make down"
	@echo ""
	cd services/frontend && $(FE_DEV_ENV) npm run dev -- --host 0.0.0.0

frontend-dev: ## Nur das Frontend im Dev-Modus (Backend muss bereits laufen)
	@cd services/frontend && [ -d node_modules ] || npm install
	@echo "Frontend dev server → http://localhost:5173"
	cd services/frontend && $(FE_DEV_ENV) npm run dev -- --host 0.0.0.0

down: ## Stop all services
	$(COMPOSE) --profile local-ollama down

logs: ## Tail logs of all services
	$(COMPOSE) --profile local-ollama logs -f

pull-models: ## Ollama-Modelle herunterladen (LLM + Embedding)
	@if docker ps --format '{{.Names}}' | grep -q ollama; then \
		echo "Lade LLM-Modell (qwen2.5:14b) ..."; \
		docker exec ollama ollama pull qwen2.5:14b; \
		echo "Lade Embedding-Modell (qwen3-embedding:0.6b) ..."; \
		docker exec ollama ollama pull qwen3-embedding:0.6b; \
		echo "Fertig."; \
	else \
		echo "Ollama-Container laeuft nicht lokal."; \
		echo "Laden Sie die Modelle direkt auf dem externen Server."; \
	fi

# ── Quality ──────────────────────────────────────────────────

test: ## Run tests for shared + Python services + frontend
	cd shared && python -m pytest -v
	@for svc in $(PY_SERVICES); do \
		echo "\n══ Testing $$svc ══"; \
		cd $(CURDIR)/services/$$svc && python -m pytest -v; \
	done
	@echo "\n══ Testing frontend ══"
	cd $(CURDIR)/services/frontend && npm run check

lint: ## Run ruff + mypy across the entire codebase
	ruff check .
	mypy shared/shared --ignore-missing-imports
	@for svc in $(PY_SERVICES); do \
		echo "\n══ Mypy $$svc ══"; \
		cd $(CURDIR)/services/$$svc && mypy main.py --ignore-missing-imports; \
	done

integration-test: ## Run E2E integration tests in Docker
	$(COMPOSE) --profile test run --rm integration-test

# ── Reset ────────────────────────────────────────────────────

reset: ## Alle Daten loeschen und Originalzustand herstellen
	@echo ""
	@echo "WARNUNG: Dies loescht ALLE Daten unwiderruflich:"
	@echo "  - PostgreSQL (Queries, Feedback, Ingestion-Log, Agent-Memory)"
	@echo "  - Qdrant (alle Vektoren und Collections)"
	@echo "  - Gespeicherte Originaldokumente"
	@echo "  - Ollama-Modelle (muessen neu geladen werden)"
	@echo ""
	@read -p "Fortfahren? [y/N] " confirm && [ "$$confirm" = "y" ] || (echo "Abgebrochen."; exit 1)
	@echo ""
	@echo "Stoppe alle Services und loesche Volumes ..."
	$(COMPOSE) --profile local-ollama --profile test down --volumes --remove-orphans
	@echo "Entferne verbleibende Container und Volumes ..."
	@docker ps -aq --filter name=kermit | xargs -r docker rm -f 2>/dev/null || true
	@docker volume ls -q --filter name=kermit | xargs -r docker volume rm -f 2>/dev/null || true
	@echo "Loesche generierte Dateien ..."
	rm -f docs/interface_agreement.html
	rm -rf services/frontend/.svelte-kit
	@echo ""
	@echo "Reset abgeschlossen. Originalzustand wiederhergestellt."
	@echo "Neu starten mit:  make dev  oder  make up-dev-full"

# ── Documentation ────────────────────────────────────────────

doc: ## HTML-Doku generieren (Services muessen laufen fuer volle Endpoint-Doku)
	@echo "Hole OpenAPI-Specs von laufenden Services ..."
	python scripts/generate_docs.py
	@echo ""
	@echo "Dokumentation generiert: docs/interface_agreement.html"
	@echo "Oeffnen:  open docs/interface_agreement.html"
	@echo ""
	@echo "Tipp: 'make dev-up' starten fuer vollstaendige Endpoint-Dokumentation"

# ── Help ─────────────────────────────────────────────────────

help: ## Show this help
	@echo "RAG Platform – Makefile Targets"
	@echo ""
	@echo "Entwicklung:"
	@echo "  make dev-full          Alle Services starten (inkl. lokales Ollama)"
	@echo "  make dev    Ohne Ollama starten (externe URL in .env.dev)"
	@echo "  make dev-fe    Backend in Docker, Frontend mit Hot-Reload (Vite)"
	@echo "  make frontend-dev    Nur Frontend im Dev-Modus (Backend muss laufen)"
	@echo "  make down        Alle Services stoppen"
	@echo "  make logs        Live-Logs aller Services"
	@echo "  make pull-models     Ollama-Modelle herunterladen"
	@echo ""
	@echo "Qualitaet:"
	@echo "  make test            Unit-Tests ausfuehren"
	@echo "  make lint            Ruff + Mypy ausfuehren"
	@echo "  make integration-test  E2E-Tests in Docker"
	@echo ""
	@echo "Dokumentation:"
	@echo "  make doc            Interface Agreement als HTML generieren"
	@echo ""
	@echo "Wartung:"
	@echo "  make reset          Alle Daten loeschen (DB, Vektoren, Dokumente)"
	@echo ""
	@echo "Konfiguration fuer dev-up-light:"
	@echo "  In .env.dev die externen URLs setzen:"
	@echo "    OLLAMA_BASE_URL=http://<externer-server>:11434"
	@echo "    USE_MINERU=true"
	@echo "    MINERU_API_URL=http://<externer-server>:8000"

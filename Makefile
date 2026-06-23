# Docker Compose V2 (Plugin) bevorzugen; sonst standalone docker-compose.
DOCKER_COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
COMPOSE = $(DOCKER_COMPOSE) -f docker-compose.dev.yml --env-file .env
PY_SERVICES = cleaning data_structure embedding vectordb evaluation

.PHONY: _ensure-env _ensure-auth-secret frontend-build up up-dev-full dev dev-bundled dev-fe frontend-dev down logs test lint integration-test pull-models doc reset help

# Backend service ports published to localhost (see docker-compose.dev.yml).
FE_DEV_ENV = \
	EVALUATION_SERVICE_URL=http://localhost:8005 \
	CLEANING_SERVICE_URL=http://localhost:8001 \
	DATA_STRUCTURE_SERVICE_URL=http://localhost:8002 \
	EMBEDDING_SERVICE_URL=http://localhost:8003 \
	VECTORDB_SERVICE_URL=http://localhost:8004

# ── Docker Compose ───────────────────────────────────────────

_ensure-env:
	@test -f .env || cp .env.example .env
	@if ! grep -q '^OLLAMA_BASE_URL=' .env 2>/dev/null; then \
		echo ""; \
		echo "FEHLER: OLLAMA_BASE_URL ist nicht in .env gesetzt."; \
		echo ""; \
		echo "Setzen Sie die URL des externen Ollama-Servers:"; \
		echo "  echo 'OLLAMA_BASE_URL=http://<HOST>:11434' >> .env"; \
		echo ""; \
		echo "Oder starten Sie mit lokalem Ollama:"; \
		echo "  make up-dev-full"; \
		echo ""; \
		exit 1; \
	fi

_ensure-auth-secret:
	@if ! grep -qE '^AUTH_SECRET=.' .env 2>/dev/null; then \
		echo "AUTH_SECRET fehlt/leer in .env – generiere einen ..."; \
		SECRET=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))'); \
		if grep -qE '^AUTH_SECRET=' .env 2>/dev/null; then \
			sed -i.bak "s|^AUTH_SECRET=.*|AUTH_SECRET=$$SECRET|" .env && rm -f .env.bak; \
		else \
			printf 'AUTH_SECRET=%s\n' "$$SECRET" >> .env; \
		fi; \
	fi

frontend-build: ## Frontend bauen (SvelteKit)
	@echo "Baue Frontend ..."
	cd services/frontend && npm install && npm run build
	@echo "Frontend-Build fertig."

up: _ensure-env _ensure-auth-secret frontend-build ## Alle Services fuer Hosting (externes Ollama, Frontend :3000)
	@echo "Starte alle Services (Ollama extern: $$(grep '^OLLAMA_BASE_URL=' .env)) ..."
	$(COMPOSE) up --build -d
	@echo ""
	@echo "Plattform laeuft:"
	@echo "  Frontend  → http://localhost:3000"
	@echo "  Logs:     make logs"
	@echo "  Stop:     make down"

up-dev-full: frontend-build ## Start all services (inkl. lokales Ollama)
	@test -f .env || cp .env.example .env
	$(COMPOSE) --profile local-ollama up --build -d
	@echo ""
	@echo "Alle Services gestartet (inkl. lokalem Ollama)."
	@echo "Ollama-Modelle laden:  make pull-models"
	@echo "Logs anzeigen:         make dev-logs"

dev: _ensure-env _ensure-auth-secret ## Backend in Docker, Frontend mit Vite-Hot-Reload auf Host (localhost:5173)
	@echo "Starte Backend-Services (ohne Frontend-Container) ..."
	$(COMPOSE) up --build -d \
		postgres qdrant cleaning data_structure embedding vectordb evaluation
	@cd services/frontend && [ -d node_modules ] || npm install
	@echo ""
	@echo "Frontend dev server mit Hot-Reload → http://localhost:5173"
	@echo "Backend-Logs:  make logs   |   Stop:  make down"
	@echo ""
	cd services/frontend && AUTH_SECRET="$$(grep -E '^AUTH_SECRET=' ../../.env 2>/dev/null | head -1 | cut -d= -f2-)" $(FE_DEV_ENV) npm run dev -- --host 0.0.0.0

dev-fe: dev ## Alias fuer 'make dev' (frueheres Verhalten beibehalten)

dev-bundled: up ## Alias fuer 'make up'

frontend-dev: ## Nur das Frontend im Dev-Modus (Backend muss bereits laufen)
	@cd services/frontend && [ -d node_modules ] || npm install
	@echo "Frontend dev server → http://localhost:5173"
	cd services/frontend && AUTH_SECRET="$$(grep -E '^AUTH_SECRET=' ../../.env 2>/dev/null | head -1 | cut -d= -f2-)" $(FE_DEV_ENV) npm run dev -- --host 0.0.0.0

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
	@echo "Neu starten mit:  make up  oder  make up-dev-full"

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
	@echo "Start:"
	@echo "  make up                Alle Services fuer Hosting (externes Ollama, :3000)"
	@echo "  make up-dev-full       Alle Services inkl. lokalem Ollama-Container"
	@echo ""
	@echo "Entwicklung:"
	@echo "  make dev               Backend in Docker + Vite-Hot-Reload (localhost:5173)"
	@echo "  make dev-bundled       Alias fuer make up"
	@echo "  make frontend-dev      Nur Frontend im Dev-Modus (Backend muss laufen)"
	@echo "  make down              Alle Services stoppen"
	@echo "  make logs              Live-Logs aller Services"
	@echo "  make pull-models       Ollama-Modelle herunterladen"
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
	@echo "  In .env die externen URLs setzen:"
	@echo "    OLLAMA_BASE_URL=http://<externer-server>:11434"
	@echo "    USE_MINERU=true"
	@echo "    MINERU_API_URL=http://<externer-server>:8000"

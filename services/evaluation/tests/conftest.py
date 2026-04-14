from __future__ import annotations

import os

os.environ.setdefault("OLLAMA_BASE_URL", "http://ollama-test:11434")
os.environ.setdefault("EMBEDDING_SERVICE_URL", "http://embedding-test:8000")
os.environ.setdefault("VECTORDB_SERVICE_URL", "http://vectordb-test:8000")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from __future__ import annotations

import os

# Set required env vars before any import of config
os.environ.setdefault("OLLAMA_BASE_URL", "http://ollama-test:11434")

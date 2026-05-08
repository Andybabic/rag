#!/usr/bin/env python3
"""Ollama API Test – liest Konfiguration direkt aus .env"""

import sys
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_FILE = Path(__file__).parent / ".env"


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


cfg = read_env(ENV_FILE)

base_url = cfg.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
api_key  = cfg.get("OLLAMA_API_KEY", "")
# model    = cfg.get("LLM_MODEL", "")
model    = "minimistral-3:14b"

print(f"Basis-URL : {base_url}")
print(f"Modell    : {model or 'nicht gesetzt'}")
print()

if not model:
    print("Kein LLM_MODEL gesetzt – Abbruch.", file=sys.stderr)
    sys.exit(1)

session = requests.Session()
session.headers["X-API-Key"] = api_key
session.verify = False

print(f"── Generate-Test mit '{model}' ─────────────────────")
try:
    r = session.post(
        f"{base_url}/api/generate",
        json={"model": model, "prompt": "Was ist 2+2? Antworte kurz.", "stream": False},
        timeout=60,
    )
    r.raise_for_status()
    print(r.json().get("response", "").strip())
except requests.HTTPError as e:
    print(f"  HTTP {e.response.status_code}: {e.response.text[:500]}", file=sys.stderr)
    sys.exit(1)
except requests.RequestException as e:
    print(f"  Fehler: {e}", file=sys.stderr)
    sys.exit(1)

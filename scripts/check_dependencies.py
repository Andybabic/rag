#!/usr/bin/env python3
"""Run Kermit's dependency self-test (LLMs, embedding, reranker) and print it.

    python3 scripts/check_dependencies.py            # waits for the service
    EVALUATION_URL=http://host:8005 python3 scripts/check_dependencies.py

Used by ``make dev`` (in the background, next to the Vite output) and
``make check``. Standard library only, so it runs on the host without a venv.
Exit code 1 if any check fails.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("EVALUATION_URL", "http://localhost:8005").rstrip("/")
# The service may still be building/starting when this runs.
STARTUP_WAIT_S = float(os.getenv("CHECK_STARTUP_WAIT", "300"))
# A cold cache downloads the reranker model first; the endpoint waits for it.
CHECK_TIMEOUT_S = float(os.getenv("CHECK_TIMEOUT", "960"))

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = DIM = RESET = ""


def _get(path: str, timeout: float) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as resp:
        return json.load(resp)


def _wait_for_service() -> bool:
    deadline = time.monotonic() + STARTUP_WAIT_S
    while time.monotonic() < deadline:
        try:
            _get("/health", timeout=5)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(2)
    return False


def _wait_for_reranker() -> None:
    """Poll while the reranker loads instead of holding one request open for
    minutes – long idle connections get dropped ("Remote end closed
    connection"), and a silent wait looks like a hang."""
    deadline = time.monotonic() + CHECK_TIMEOUT_S
    announced = 0.0
    while time.monotonic() < deadline:
        try:
            status = _get("/health/reranker", timeout=10)
        except (urllib.error.URLError, OSError, ValueError):
            return  # the dependency check itself will report what is wrong
        if status.get("state") not in ("loading", "not_started"):
            return
        if time.monotonic() - announced > 60:
            print(
                f"{DIM}[Selbsttest] Reranker {status.get('model')} lädt noch "
                f"(Download/Laden, kann dauern) …{RESET}",
                flush=True,
            )
            announced = time.monotonic()
        time.sleep(5)


def main() -> int:
    if not _wait_for_service():
        print(f"{RED}[Selbsttest] Evaluation-Service unter {BASE} nicht erreichbar.{RESET}")
        return 1
    _wait_for_reranker()
    print(f"{DIM}[Selbsttest] prüfe LLM, Embedding und Reranker …{RESET}", flush=True)
    try:
        # The startup run may still hold the lock; it finishes shortly once
        # the reranker is loaded, so a few minutes is plenty here.
        result = _get("/health/dependencies?refresh=true", timeout=300)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"{DIM}[Selbsttest] Service hat keinen Selbsttest – übersprungen.{RESET}")
            return 0
        print(f"{RED}[Selbsttest] HTTP {exc.code}: {exc.read()[:300]!r}{RESET}")
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"{RED}[Selbsttest] fehlgeschlagen: {exc}{RESET}")
        return 1

    lines = ["", "[Selbsttest] Kermit-Abhängigkeiten:"]
    for c in result.get("checks", []):
        if c.get("skipped"):
            mark = f"{DIM}  –{RESET}"
        elif c.get("ok"):
            mark = f"{GREEN}  ✓{RESET}"
        else:
            mark = f"{RED}  ✗{RESET}"
        use_cases = ", ".join(c.get("use_cases") or [])
        scope = f" {DIM}[{use_cases}]{RESET}" if use_cases and use_cases != "*" else ""
        lines.append(f"{mark} {c['component']:<10} {c.get('name') or ''}{scope}")
        lines.append(f"      {c.get('message', '')}")
        if not c.get("ok") and c.get("hint"):
            lines.append(f"      {RED}Hinweis: {c['hint']}{RESET}")
    ok = bool(result.get("ok"))
    lines.append(
        f"{GREEN}[Selbsttest] alles erreichbar.{RESET}"
        if ok
        else f"{RED}[Selbsttest] FEHLER – Kermit wird mit diesen Einstellungen schlecht "
        f"oder gar nicht antworten.{RESET}"
    )
    print("\n".join(lines) + "\n", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

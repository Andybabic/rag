"""Startup self-test: are the LLMs, embeddings and the reranker actually usable?

Kermit starts happily with a wrong API key, an unreachable GPU host or a
reranker that loaded with a random head – and then answers every question
with "keine Information". This runs once at startup (in the background, so a
dead provider never blocks the service), logs every failure as ERROR, and keeps
the result for ``GET /health/dependencies``. The benchmark refuses to start a
run while it reports a failure.

Checked:
- LLM: every distinct chat/vision setup across the global config and all use
  cases, via the same connection test the settings page uses (model list +
  one tiny chat call).
- Embedding: one embed call per use case through the embedding service.
- Reranker: waits for the model to load, then scores a relevant against an
  irrelevant passage. A reranker that cannot tell them apart is reported as
  broken, not just one that crashes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime

import httpx
from shared.usecase_config import list_use_cases, resolve_config

from config import settings
from core.provider_test import run_connection_tests
from core.reranker import probe_reranker, reranker_status

logger = logging.getLogger(__name__)

STARTUP_CHECKS_ENABLED = os.getenv("STARTUP_CHECKS", "true").lower() in ("1", "true", "yes")
# A cold HF cache downloads the reranker first; give it time before calling it failed.
_RERANKER_LOAD_TIMEOUT_S = float(os.getenv("STARTUP_CHECK_RERANKER_WAIT", "900") or 900)

_last: dict | None = None
_lock = asyncio.Lock()


def last_result() -> dict | None:
    return _last


async def _use_case_ids() -> list[str]:
    try:
        return [uc.id for uc in await list_use_cases()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup check: use cases not readable (%s); global config only", exc)
        return []


async def _check_llms(use_cases: list[str]) -> list[dict]:
    """One connection test per distinct setup – use cases usually share one."""
    setups: dict[tuple, dict] = {}
    for uc in [None, *use_cases]:
        cfg = await resolve_config(uc)
        key = (
            cfg.chat_provider, cfg.ollama_base_url, cfg.openai_base_url, cfg.llm_model,
            cfg.vision_provider, cfg.vision_model,
        )
        setups.setdefault(key, {"cfg": cfg, "use_cases": []})["use_cases"].append(uc or "*")

    async def one(entry: dict) -> list[dict]:
        result = await run_connection_tests(entry["cfg"])
        return [
            {
                "component": f"llm.{c['role']}",
                "name": f"{c.get('model') or '?'} @ {c.get('base_url')}",
                "use_cases": entry["use_cases"],
                "ok": bool(c.get("ok")),
                "skipped": bool(c.get("skipped")),
                "message": c.get("message", ""),
                "hint": c.get("hint", ""),
                "latency_ms": c.get("latency_ms"),
            }
            for c in result["checks"]
        ]

    nested = await asyncio.gather(*(one(e) for e in setups.values()))
    return [c for group in nested for c in group]


async def _check_embedding(use_cases: list[str]) -> list[dict]:
    async def one(client: httpx.AsyncClient, uc: str | None) -> dict:
        started = time.monotonic()
        check = {"component": "embedding", "use_cases": [uc or "*"], "ok": False}
        try:
            resp = await client.post(
                f"{settings.EMBEDDING_SERVICE_URL}/v1/embed",
                json={"type": "text", "content": "Startup-Check", "use_case": uc},
            )
            resp.raise_for_status()
            body = resp.json()
            check.update(
                ok=bool(body.get("vector")),
                name=body.get("model"),
                message=f"{body.get('model')} liefert {body.get('dimension')} Dimensionen",
            )
        except Exception as exc:  # noqa: BLE001
            check.update(name="embedding-service", message=f"{type(exc).__name__}: {exc}")
        check["latency_ms"] = int((time.monotonic() - started) * 1000)
        return check

    async with httpx.AsyncClient(timeout=60.0) as client:
        results = await asyncio.gather(*(one(client, uc) for uc in [None, *use_cases]))

    # Many use cases share one model: report each model once, failures per use case.
    merged: dict[tuple, dict] = {}
    for r in results:
        key = (r["ok"], r.get("name"), r["message"] if not r["ok"] else "")
        if key in merged:
            merged[key]["use_cases"] += r["use_cases"]
        else:
            merged[key] = r
    return list(merged.values())


async def _check_reranker() -> dict:
    status = reranker_status()
    check = {"component": "reranker", "name": status["model"], "use_cases": ["*"]}
    if not status["enabled"]:
        return {**check, "ok": True, "skipped": True, "message": "Reranker ausgeschaltet"}

    deadline = time.monotonic() + _RERANKER_LOAD_TIMEOUT_S
    while reranker_status()["state"] in ("loading", "not_started") and time.monotonic() < deadline:
        await asyncio.sleep(2)
    probe = await asyncio.to_thread(probe_reranker)
    return {**check, **probe}


async def run_checks() -> dict:
    """Run all checks, log the outcome and keep it for the health endpoint."""
    global _last  # noqa: PLW0603
    async with _lock:
        started = time.monotonic()
        use_cases = await _use_case_ids()
        llm, embedding = await asyncio.gather(_check_llms(use_cases), _check_embedding(use_cases))
        checks = [*llm, *embedding, await _check_reranker()]
        for c in checks:
            if c.get("ok"):
                logger.info("Startup check OK   %s %s: %s", c["component"], c.get("name"), c.get("message"))
            else:
                logger.error(
                    "Startup check FAIL %s %s (use cases %s): %s %s",
                    c["component"], c.get("name"), ", ".join(c.get("use_cases", [])),
                    c.get("message"), c.get("hint", ""),
                )
        _last = {
            "ok": all(c.get("ok") for c in checks),
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "checks": checks,
        }
        return _last


async def run_checks_in_background() -> None:
    try:
        await run_checks()
    except Exception:  # noqa: BLE001
        # A bug in the self-test must never take the service down.
        logger.exception("Startup check crashed")

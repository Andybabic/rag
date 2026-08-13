"""Probe LLM provider endpoints and turn failures into German UI copy.

Used by the settings page so admins can verify Base-URL, API-Key and model
before chatting — and see *why* a connection failed, not a raw stack trace.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from typing import Any, Literal

from shared.llm.config import LLMConfig
from shared.llm.registry import get_provider

Role = Literal["chat", "vision"]

_STATUS_RE = re.compile(r"returned\s+(\d{3})\b")
_SECRET_RE = re.compile(
    r"(?i)(?:sk-|ak_|Bearer\s+|api[_-]?key[=:\s]+)[A-Za-z0-9_\-]{8,}"
)
_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)

_PING_TIMEOUT_S = 45.0
_PING_PROMPT = "Reply with the single word pong."


def overlay_config(base: LLMConfig, patch: dict[str, Any]) -> LLMConfig:
    """Apply unsaved settings-form values on top of the resolved config.

    Empty strings are ignored so an unedited field keeps the stored/.env value.
    API keys overlay only when the user typed a new one.
    """
    fields: dict[str, Any] = {}
    for key in (
        "chat_provider",
        "vision_provider",
        "ollama_base_url",
        "openai_base_url",
        "llm_model",
        "vision_model",
    ):
        val = patch.get(key)
        if isinstance(val, str) and val.strip():
            fields[key] = val.strip()
    for key in ("ollama_api_key", "openai_api_key"):
        val = patch.get(key)
        if isinstance(val, str) and val.strip():
            fields[key] = val.strip()
    return replace(base, **fields) if fields else base


def _base_url_for(cfg: LLMConfig, provider: str) -> str:
    if provider == "openai":
        return cfg.openai_base_url
    return cfg.ollama_base_url


def _redact(text: str) -> str:
    text = _SECRET_RE.sub("***", text)
    if len(text) > 600:
        return text[:600] + "…"
    return text


def _http_status(text: str) -> int | None:
    m = _STATUS_RE.search(text)
    return int(m.group(1)) if m else None


def _error_payload(text: str) -> dict[str, Any]:
    """Best-effort parse of a JSON error body embedded in an exception message."""
    m = _JSON_RE.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return data["error"]
    return data if isinstance(data, dict) else {}


def explain_provider_error(
    exc: BaseException,
    *,
    provider: str,
    base_url: str,
    model: str | None = None,
) -> dict[str, str]:
    """Map a provider exception to ``code`` / ``message`` / ``hint`` / ``detail``."""
    raw = _redact(str(exc).strip() or exc.__class__.__name__)
    lowered = raw.lower()
    status = _http_status(raw)
    payload = _error_payload(raw)
    payload_code = str(payload.get("code") or payload.get("type") or "").lower()
    payload_msg = str(payload.get("message") or "").strip()

    def result(code: str, message: str, hint: str, detail: str | None = None) -> dict[str, str]:
        return {
            "code": code,
            "message": message,
            "hint": hint,
            "detail": detail or (payload_msg or raw),
        }

    if isinstance(exc, ValueError) and "unknown llm provider" in lowered:
        return result(
            "unknown_provider",
            f"Unbekannter Provider „{provider}“.",
            "Wählen Sie in den Einstellungen ollama oder openai.",
        )

    connect_like = any(
        token in lowered
        for token in (
            "connecterror",
            "connection refused",
            "connect call failed",
            "name or service not known",
            "nodename nor servname",
            "name resolution",
            "network is unreachable",
            "not reachable",
        )
    )
    timeout_like = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or any(
        token in lowered
        for token in ("timed out after", "timeoutexception", "read timeout", "write timeout")
    )

    if timeout_like and not connect_like:
        return result(
            "timeout",
            f"Zeitüberschreitung beim Verbinden mit {base_url}.",
            "Der Server antwortet nicht rechtzeitig. Prüfen Sie Last, Base-URL "
            "und ob das Modell erst geladen werden muss.",
        )

    if connect_like:
        return result(
            "unreachable",
            f"Der Server unter {base_url} ist nicht erreichbar.",
            "Läuft der Dienst? Stimmt die Base-URL (Host, Port, Pfad)? "
            "Ist das Netzwerk bzw. die Firewall offen?",
        )

    if timeout_like or "timeout" in lowered or "timed out" in lowered:
        return result(
            "timeout",
            f"Zeitüberschreitung beim Verbinden mit {base_url}.",
            "Der Server antwortet nicht rechtzeitig. Prüfen Sie Last, Base-URL "
            "und ob das Modell erst geladen werden muss.",
        )

    if any(
        token in lowered
        for token in ("ssl", "certificate_verify_failed", "certificate verify")
    ):
        return result(
            "ssl",
            f"TLS/SSL-Fehler beim Verbinden mit {base_url}.",
            "Das Zertifikat ist ungültig oder HTTP/HTTPS passt nicht zur URL.",
        )

    if "openai_api_key is required" in lowered or (
        provider == "openai" and "api key is required" in lowered
    ):
        return result(
            "missing_key",
            "Für den OpenAI-Provider ist ein API-Key erforderlich.",
            "Tragen Sie den Key unter „Endpunkte & Keys“ ein und speichern Sie "
            "ihn, oder testen Sie mit dem gerade eingegebenen Key.",
        )

    if (
        status in (401, 403)
        or payload_code in {"invalid_api_key", "invalid_key", "authentication_error"}
        or "incorrect api key" in lowered
        or "invalid api key" in lowered
        or "unauthorized" in lowered
    ):
        auth_hint = (
            "Der API-Key ist ungültig, abgelaufen oder fehlt. "
            "Prüfen Sie den Key unter „Endpunkte & Keys“."
        )
        if status == 403 or "permission" in lowered or "forbidden" in lowered:
            return result(
                "forbidden",
                "Zugriff verweigert.",
                "Der Key hat keine Berechtigung für diesen Endpunkt oder dieses Modell.",
            )
        return result("auth_failed", "Authentifizierung fehlgeschlagen.", auth_hint)

    if (
        payload_code in {"insufficient_quota", "billing_not_active"}
        or "insufficient_quota" in lowered
        or "exceeded your current quota" in lowered
    ):
        return result(
            "quota",
            "Das Kontingent beim Anbieter ist aufgebraucht.",
            "Prüfen Sie Abrechnung, Limits und ob der Key zu einem aktiven Projekt gehört.",
        )

    if status == 429 or "rate limit" in lowered or payload_code == "rate_limit_exceeded":
        return result(
            "rate_limited",
            "Der Anbieter hat die Anfrage wegen zu vieler Requests abgelehnt (Rate-Limit).",
            "Warten Sie kurz und versuchen Sie es erneut.",
        )

    model_missing = (
        payload_code in {"model_not_found", "invalid_model"}
        or "model_not_found" in lowered
        or "does not exist" in lowered
        or (status == 404 and model and model.lower() in lowered)
    )
    if model_missing and model:
        return result(
            "model_not_found",
            f"Das Modell „{model}“ ist auf diesem Server nicht verfügbar.",
            "Wählen Sie ein vorhandenes Modell oder ziehen Sie es zuerst "
            "(bei Ollama z. B. per `ollama pull`).",
        )

    if status == 404:
        missing_v1 = provider == "openai" and "/v1" not in (base_url or "").rstrip("/")
        hint = (
            "OpenAI-kompatible Server erwarten die Base-URL mit `/v1` "
            f"(z. B. {base_url.rstrip('/')}/v1). Ohne diesen Pfad landet Chat auf "
            "/chat/completions statt /v1/chat/completions."
            if missing_v1
            else "Prüfen Sie die Base-URL — bei OpenAI-kompatiblen Servern endet sie "
            "typischerweise auf `/v1`, bei Ollama ohne Pfad (Port 11434)."
        )
        return result(
            "not_found",
            f"Endpunkt nicht gefunden unter {base_url}.",
            hint,
        )

    if status is not None and status >= 500:
        return result(
            "server_error",
            f"Der Anbieter hat einen internen Fehler gemeldet (HTTP {status}).",
            "Der Dienst läuft, ist aber gerade nicht in der Lage zu antworten. "
            "Logs des Providers prüfen und später erneut testen.",
        )

    if status is not None:
        return result(
            "http_error",
            f"Der Anbieter hat HTTP {status} zurückgegeben.",
            payload_msg or "Details stehen in der technischen Meldung unten.",
        )

    return result(
        "unknown",
        "Die Verbindung zum Anbieter ist fehlgeschlagen.",
        "Prüfen Sie Base-URL, API-Key und Modell. Die technische Meldung "
        "unten kann den genauen Grund enthalten.",
    )


def _model_names(raw: list[dict]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = item.get("name") or item.get("id") or ""
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    names.sort()
    return names


def _ok_message(role: Role, provider: str, names: list[str], model: str | None, ping: bool) -> str:
    n = len(names)
    models_part = "1 Modell gefunden" if n == 1 else f"{n} Modelle gefunden"
    if role == "vision":
        if model:
            return (
                f"Vision-Endpunkt erreichbar ({provider}). {models_part}. "
                f"Modell „{model}“ ist konfiguriert."
            )
        return f"Vision-Endpunkt erreichbar ({provider}). {models_part}."
    if ping and model:
        return (
            f"Verbindung erfolgreich ({provider}). {models_part}, "
            f"Chat-Ping mit „{model}“ hat geantwortet."
        )
    if model:
        return (
            f"Endpunkt erreichbar ({provider}). {models_part}. "
            f"Chat-Ping mit „{model}“ wurde nicht ausgeführt."
        )
    return (
        f"Endpunkt erreichbar ({provider}). {models_part}. "
        "Kein Chat-Modell gesetzt — Ping übersprungen."
    )


async def _check_role(cfg: LLMConfig, role: Role) -> dict[str, Any]:
    provider_name = cfg.chat_provider if role == "chat" else cfg.vision_provider
    model = cfg.llm_model if role == "chat" else cfg.vision_model
    base_url = _base_url_for(cfg, provider_name)
    started = time.monotonic()

    check: dict[str, Any] = {
        "role": role,
        "ok": False,
        "skipped": False,
        "provider": provider_name,
        "base_url": base_url,
        "model": model,
        "models": [],
        "latency_ms": 0,
        "code": "unknown",
        "message": "",
        "hint": "",
        "detail": "",
    }

    def finish() -> dict[str, Any]:
        check["latency_ms"] = int((time.monotonic() - started) * 1000)
        return check

    try:
        provider = get_provider(provider_name, cfg)
    except ValueError as exc:
        check.update(
            explain_provider_error(exc, provider=provider_name, base_url=base_url, model=model)
        )
        return finish()

    try:
        raw = await provider.list_models()
        names = _model_names(raw if isinstance(raw, list) else [])
        check["models"] = names
    except Exception as exc:
        check.update(
            explain_provider_error(exc, provider=provider_name, base_url=base_url, model=model)
        )
        return finish()

    ping_ok = False
    if role == "chat" and model:
        try:
            await asyncio.wait_for(
                provider.chat(
                    [{"role": "user", "content": _PING_PROMPT}],
                    model=model,
                    options={"temperature": 0, "max_tokens": 8},
                ),
                timeout=_PING_TIMEOUT_S,
            )
            ping_ok = True
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError):
                exc = TimeoutError(f"timed out after {_PING_TIMEOUT_S:.0f}s at {base_url}")
            check.update(
                explain_provider_error(
                    exc, provider=provider_name, base_url=base_url, model=model
                )
            )
            check["hint"] = (
                check["hint"]
                + " Die Modellliste war erreichbar — das Problem betrifft den Chat-Aufruf."
            ).strip()
            return finish()
    elif role == "chat" and not model:
        check["hint"] = "Wählen Sie ein LLM-Modell, um zusätzlich den Chat-Aufruf zu prüfen."
    elif role == "vision" and model and names and model not in names:
        check["hint"] = (
            f"„{model}“ steht nicht in der Modellliste. Der Endpunkt ist erreichbar, "
            "aber Vision könnte mit diesem Namen scheitern."
        )

    check["ok"] = True
    check["code"] = "ok"
    check["message"] = _ok_message(role, provider_name, names, model, ping_ok)
    return finish()


async def run_connection_tests(cfg: LLMConfig) -> dict[str, Any]:
    """Test chat (and vision if it uses a different endpoint)."""
    chat = await _check_role(cfg, "chat")
    checks = [chat]

    same_endpoint = (
        cfg.vision_provider == cfg.chat_provider
        and _base_url_for(cfg, cfg.vision_provider) == _base_url_for(cfg, cfg.chat_provider)
    )
    if same_endpoint:
        vision = {
            "role": "vision",
            "ok": chat["ok"],
            "skipped": True,
            "provider": chat["provider"],
            "base_url": chat["base_url"],
            "model": cfg.vision_model,
            "models": chat.get("models") or [],
            "latency_ms": chat["latency_ms"],
            "code": "skipped_same_endpoint",
            "message": (
                "Vision nutzt denselben Endpunkt wie Chat — Verbindung bereits geprüft."
                if chat["ok"]
                else "Vision nutzt denselben Endpunkt wie Chat — siehe Chat-Fehler."
            ),
            "hint": "",
            "detail": "",
        }
        checks.append(vision)
    else:
        checks.append(await _check_role(cfg, "vision"))

    ok = all(c.get("ok") for c in checks if not c.get("skipped"))
    return {"ok": ok, "checks": checks}


async def list_chat_models(cfg: LLMConfig) -> dict[str, Any]:
    """List models on the chat provider, with the same error mapping as the test."""
    provider_name = cfg.chat_provider
    base_url = _base_url_for(cfg, provider_name)
    try:
        provider = get_provider(provider_name, cfg)
        raw = await provider.list_models()
        return {"models": _model_names(raw if isinstance(raw, list) else [])}
    except Exception as exc:
        explained = explain_provider_error(
            exc, provider=provider_name, base_url=base_url, model=cfg.llm_model
        )
        return {"models": [], "error": explained["code"], **explained}

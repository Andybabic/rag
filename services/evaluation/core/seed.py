"""Seed the DB-backed use-case registry from ``config/use_cases.json``.

Runs at service startup. Idempotent and **create-if-missing**: a use case that
already exists in the DB (e.g. edited via the dashboard) is left untouched.

System prompts are the exception, because create-if-missing alone made every
later edit to the JSON invisible: the DB row existed, so the file was never
read again — prompt rules were written, deployed, and silently never reached
a single request. A prompt is therefore updated from the file as long as the
DB still holds exactly what was seeded there last time. The seeded text is
recorded as a hash next to the prompt; once the stored text no longer matches
it, someone edited it in the dashboard, and their version wins for good.

The config path is ``config/use_cases.json`` relative to the service root, or
the ``USE_CASES_CONFIG_PATH`` env var if set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from shared.usecase_config import (
    get_use_case,
    list_deleted_use_cases,
    list_prompts,
    upsert_prompt,
    upsert_use_case,
)

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "use_cases.json"


def _config_path() -> Path:
    override = os.getenv("USE_CASES_CONFIG_PATH")
    return Path(override) if override else _DEFAULT_PATH


def _system_prompt_key(role: str) -> str:
    return f"agent.system.{role}"


def _seed_marker_key(role: str) -> str:
    """Wo der Fingerabdruck des zuletzt geseedeten Prompts liegt.

    Eigener Schlüssel in derselben Tabelle statt einer neuen Spalte: Das
    Dashboard liest ausschliesslich ``agent.system.<rolle>`` und zeigt diesen
    Eintrag deshalb nicht an.
    """
    return f"agent.system.{role}.seeded_sha"


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


async def _seed_prompt(uc_id: str, role: str, text: str, db_prompts: dict) -> str:
    """Einen System-Prompt seeden. Gibt zurück, was geschah (für das Log)."""
    key, marker = _system_prompt_key(role), _seed_marker_key(role)
    stored = db_prompts.get(key)
    fingerprint = _fingerprint(text)

    if stored is None:
        await upsert_prompt(uc_id, key, text)
        await upsert_prompt(uc_id, marker, fingerprint)
        return "created"

    if _fingerprint(stored) == fingerprint:
        # Inhaltlich gleich – nur der Fingerabdruck fehlt womöglich noch,
        # etwa bei einem Bestand aus der Zeit vor dieser Logik.
        if db_prompts.get(marker) != fingerprint:
            await upsert_prompt(uc_id, marker, fingerprint)
        return "unchanged"

    seeded = db_prompts.get(marker)
    if seeded and seeded == _fingerprint(stored):
        # In der DB steht noch exakt der zuletzt geseedete Text – niemand hat
        # ihn angefasst, also darf die Datei ihn ablösen.
        await upsert_prompt(uc_id, key, text)
        await upsert_prompt(uc_id, marker, fingerprint)
        return "updated"

    return "kept"


async def seed_use_cases(path: str | Path | None = None) -> int:
    """Seed missing use cases + prompts from the config file.

    Returns the number of newly created use cases (0 if all already existed or
    the config file is absent).
    """
    p = Path(path) if path else _config_path()
    if not p.exists():
        logger.warning("Use-case config not found at %s — skipping seed", p)
        return 0

    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.error("Failed to read use-case config %s: %s", p, exc)
        return 0

    cases = doc.get("use_cases", [])
    # Use cases the admin deleted via the dashboard must NOT be resurrected on
    # the next startup. Fetch the tombstones once and skip those ids entirely.
    deleted = await list_deleted_use_cases()
    created = 0
    for uc in cases:
        uc_id = uc.get("id")
        if not uc_id:
            logger.warning("Skipping use-case entry without 'id': %r", uc)
            continue

        if uc_id in deleted:
            logger.info("Skipping seed of deleted use case %r (tombstoned)", uc_id)
            continue

        if await get_use_case(uc_id) is None:
            await upsert_use_case(
                uc_id,
                slug=uc["slug"],
                label=uc["label"],
                description=uc.get("description", ""),
                color=uc.get("color", ""),
                accent=uc.get("accent", ""),
                roles=uc["roles"],
                agent_action_names=uc["agent_action_names"],
                default_collection=uc["default_collection"],
                collection_prefixes=uc.get("collection_prefixes") or [],
                enabled=uc.get("enabled", True),
            )
            created += 1

        db_prompts = await list_prompts(uc_id)
        for role, text in (uc.get("prompts") or {}).items():
            if not text:
                continue
            outcome = await _seed_prompt(uc_id, role, text, db_prompts)
            if outcome == "updated":
                logger.info(
                    "System prompt %s/%s updated from config (DB still held the "
                    "seeded version)", uc_id, role,
                )
            elif outcome == "kept":
                # Kein Fehler, aber der häufigste Grund für „meine
                # Prompt-Änderung wirkt nicht": Im Dashboard bearbeitet, damit
                # hat die DB Vorrang.
                logger.warning(
                    "System prompt %s/%s differs from config and was edited in "
                    "the dashboard — the stored version stays in effect; the "
                    "config change has NO effect until it is cleared there",
                    uc_id, role,
                )

    logger.info("Use-case seed complete: %d new use case(s) from %s", created, p)
    return created

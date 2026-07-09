"""Seed the DB-backed use-case registry from ``config/use_cases.json``.

Runs at service startup. Idempotent and **create-if-missing**: a use case that
already exists in the DB (e.g. edited via the dashboard) is left untouched, and
a system prompt is only written when no DB override exists yet. This makes the
JSON the initial source of truth while letting the dashboard own the live state.

The config path is ``config/use_cases.json`` relative to the service root, or
the ``USE_CASES_CONFIG_PATH`` env var if set.
"""

from __future__ import annotations

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

        # Seed system prompts only where the DB has no override yet.
        db_prompts = await list_prompts(uc_id)
        for role, text in (uc.get("prompts") or {}).items():
            key = _system_prompt_key(role)
            if text and key not in db_prompts:
                await upsert_prompt(uc_id, key, text)

    logger.info("Use-case seed complete: %d new use case(s) from %s", created, p)
    return created

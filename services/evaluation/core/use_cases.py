"""Use-case metadata lookup.

System prompts and the global ReAct suffix are now resolved through the
DB-backed ``shared.usecase_config`` resolver — values stored there override
the file-loaded defaults. Editing happens via the admin API, which writes
back through this module.

Agent action toggles (enabled flag + description) remain in-memory for now
(they reset to defaults on process restart).
"""

from __future__ import annotations

import copy
import logging

from core.prompts import ACTION_CATALOG
from core.prompts import AGENT_REACT_SUFFIX as _DEFAULT_REACT_SUFFIX
from core.prompts import USE_CASE_META as _FILE_USE_CASE_META
from core.prompts import ActionDef
from shared.usecase_config import (
    GLOBAL_USE_CASE,
    list_prompts,
    list_use_cases,
    resolve_prompt,
    upsert_prompt,
)

logger = logging.getLogger(__name__)

# Mutable in-memory snapshot of the use-case registry. Starts from the file
# defaults (config/use_cases.json carries the same values and is seeded into the
# DB at boot) and is replaced by the DB state via ``refresh_use_cases()`` —
# called at startup after seeding and after any dashboard write.
_USE_CASE_META: dict[str, dict] = copy.deepcopy(_FILE_USE_CASE_META)

# Prompt-key conventions used in the usecase_prompts table:
#   agent.system.{role}            (per-usecase agent system prompt)
#   agent.react_suffix             (use_case = '*'  — global ReAct suffix)
PROMPT_KEY_REACT_SUFFIX = "agent.react_suffix"


def _system_prompt_key(role: str) -> str:
    return f"agent.system.{role}"


def get_use_case_meta(use_case: str) -> dict:
    """Return metadata for a use case. Raises ValueError if unknown."""
    if use_case not in _USE_CASE_META:
        available = ", ".join(_USE_CASE_META.keys())
        raise ValueError(f"Unbekannter Use Case '{use_case}'. Verfügbar: {available}")
    return _USE_CASE_META[use_case]


def _file_default_prompt(use_case: str, role: str) -> str:
    meta = get_use_case_meta(use_case)
    prompts = meta["system_prompt"]
    return prompts.get(role, prompts["default"])


async def get_system_prompt(use_case: str, role: str = "default") -> str:
    """Get the effective system prompt: DB override → file default."""
    default = _file_default_prompt(use_case, role)
    value = await resolve_prompt(
        use_case, _system_prompt_key(role), default=default
    )
    return value or default


async def set_system_prompt(use_case: str, role: str, prompt: str) -> None:
    """Persist a system-prompt override (empty string clears it)."""
    get_use_case_meta(use_case)  # raise on unknown use case
    await upsert_prompt(use_case, _system_prompt_key(role), prompt)


async def get_react_suffix() -> str:
    """Get the ReAct suffix: DB global override → file default."""
    value = await resolve_prompt(
        GLOBAL_USE_CASE, PROMPT_KEY_REACT_SUFFIX, default=_DEFAULT_REACT_SUFFIX
    )
    return value or _DEFAULT_REACT_SUFFIX


async def set_react_suffix(content: str) -> None:
    """Persist a ReAct suffix override (empty string clears it)."""
    await upsert_prompt(GLOBAL_USE_CASE, PROMPT_KEY_REACT_SUFFIX, content)


def get_agent_actions(use_case: str) -> list[str]:
    """Get *enabled* agent action names for a use case."""
    actions = get_use_case_meta(use_case)["agent_actions"]
    return [name for name, defn in actions.items() if defn["enabled"]]


def get_agent_actions_full(use_case: str) -> dict[str, ActionDef]:
    """Get the full actions dict (name → {enabled, description}) for a use case."""
    return get_use_case_meta(use_case)["agent_actions"]


def set_action_enabled(use_case: str, action_name: str, enabled: bool) -> None:
    """Enable or disable a single action for a use case (runtime only)."""
    actions = get_use_case_meta(use_case)["agent_actions"]
    if action_name not in actions:
        available = ", ".join(actions.keys())
        raise ValueError(
            f"Aktion '{action_name}' existiert nicht für Use Case '{use_case}'. "
            f"Verfügbar: {available}"
        )
    actions[action_name]["enabled"] = enabled


def set_action_description(use_case: str, action_name: str, description: str) -> None:
    """Update the description of an action for a use case (runtime only)."""
    actions = get_use_case_meta(use_case)["agent_actions"]
    if action_name not in actions:
        raise ValueError(f"Aktion '{action_name}' existiert nicht für Use Case '{use_case}'.")
    actions[action_name]["description"] = description


def get_default_collection(use_case: str) -> str:
    """Get the default collection for a use case."""
    return get_use_case_meta(use_case)["default_collection"]


# ── Collection-Prefix-Isolation ──────────────────────────────────────────────

# File-default prefixes (fallback if the DB is empty/unreachable). Refreshed
# from the DB by ``refresh_use_cases()``.
_FILE_PREFIXES: dict[str, list[str]] = {
    "neumann": ["neumann_"],
    "gw_stpoelten": ["gw_"],
    "wiener_linien": ["wl_"],
    "ustp": ["ustp_"],
}
_USE_CASE_PREFIXES: dict[str, list[str]] = dict(_FILE_PREFIXES)


def get_use_case_prefixes(use_case: str) -> list[str]:
    """Return the allowed collection-name prefixes for a use case.

    Used to enforce hard isolation – a use case may ONLY access collections
    whose names start with one of these prefixes.
    """
    return _USE_CASE_PREFIXES.get(use_case, [use_case + "_"])


def collection_belongs_to_use_case(collection: str, use_case: str) -> bool:
    """Check whether a collection name is allowed for the given use case."""
    return any(collection.startswith(p) for p in get_use_case_prefixes(use_case))


# ── DB refresh ────────────────────────────────────────────────────────────────


def _build_actions(names: list[str]) -> dict[str, ActionDef]:
    return {
        name: ActionDef(enabled=True, description=ACTION_CATALOG.get(name, ""))
        for name in names
    }


async def refresh_use_cases() -> int:
    """Rebuild the in-memory registry (meta + prefixes) from the DB.

    Call at startup (after seeding) and after dashboard writes. Falls back to
    the file defaults — leaving the current snapshot untouched — if the DB is
    empty or unreachable. Returns the number of use cases loaded.
    """
    global _USE_CASE_META, _USE_CASE_PREFIXES

    cases = await list_use_cases()
    if not cases:
        logger.info("refresh_use_cases: DB empty/unreachable — keeping file defaults")
        return 0

    new_meta: dict[str, dict] = {}
    new_prefixes: dict[str, list[str]] = {}
    for uc in cases:
        db_prompts = await list_prompts(uc.id)
        file_prompts = _FILE_USE_CASE_META.get(uc.id, {}).get("system_prompt", {})
        roles = uc.roles or ["default"]
        system_prompt: dict[str, str] = {}
        for role in roles:
            key = _system_prompt_key(role)
            system_prompt[role] = db_prompts.get(key) or file_prompts.get(role, "")
        # _file_default_prompt() always indexes ["default"], so guarantee it.
        if "default" not in system_prompt:
            system_prompt["default"] = file_prompts.get("default", "")

        new_meta[uc.id] = {
            "system_prompt": system_prompt,
            "agent_actions": _build_actions(uc.agent_action_names),
            "default_collection": uc.default_collection,
        }
        new_prefixes[uc.id] = uc.collection_prefixes or [uc.id + "_"]

    _USE_CASE_META = new_meta
    _USE_CASE_PREFIXES = new_prefixes
    logger.info("refresh_use_cases: loaded %d use case(s) from DB", len(cases))
    return len(cases)

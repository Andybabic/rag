"""Use-case metadata lookup.

System prompts and the global ReAct suffix are now resolved through the
DB-backed ``shared.usecase_config`` resolver — values stored there override
the file-loaded defaults. Editing happens via the admin API, which writes
back through this module.

Agent action toggles (enabled flag + description) remain in-memory for now
(they reset to defaults on process restart).
"""

from __future__ import annotations

from core.prompts import AGENT_REACT_SUFFIX as _DEFAULT_REACT_SUFFIX
from core.prompts import USE_CASE_META as _USE_CASE_META
from core.prompts import ActionDef
from shared.usecase_config import (
    GLOBAL_USE_CASE,
    resolve_prompt,
    upsert_prompt,
)

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

_USE_CASE_PREFIXES: dict[str, list[str]] = {
    "neumann": ["neumann_"],
    "gw_stpoelten": ["gw_"],
    "wiener_linien": ["wl_"],
    "ustp": ["ustp_"],
}


def get_use_case_prefixes(use_case: str) -> list[str]:
    """Return the allowed collection-name prefixes for a use case.

    Used to enforce hard isolation – a use case may ONLY access collections
    whose names start with one of these prefixes.
    """
    return _USE_CASE_PREFIXES.get(use_case, [use_case + "_"])


def collection_belongs_to_use_case(collection: str, use_case: str) -> bool:
    """Check whether a collection name is allowed for the given use case."""
    return any(collection.startswith(p) for p in get_use_case_prefixes(use_case))

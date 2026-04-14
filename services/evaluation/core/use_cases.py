"""Use-case metadata lookup.

Provides system prompts and agent actions per use case.
In production, this could call the data_structure service.
For now, provides built-in defaults for the three known use cases.
"""

from __future__ import annotations

from core.prompts import USE_CASE_META as _USE_CASE_META
from core.prompts import ActionDef


def get_use_case_meta(use_case: str) -> dict:
    """Return metadata for a use case. Raises ValueError if unknown."""
    if use_case not in _USE_CASE_META:
        available = ", ".join(_USE_CASE_META.keys())
        raise ValueError(f"Unbekannter Use Case '{use_case}'. Verfügbar: {available}")
    return _USE_CASE_META[use_case]


def get_system_prompt(use_case: str, role: str = "default") -> str:
    """Get the system prompt for a use case and role."""
    meta = get_use_case_meta(use_case)
    prompts = meta["system_prompt"]
    return prompts.get(role, prompts["default"])


def set_system_prompt(use_case: str, role: str, prompt: str) -> None:
    """Update the system prompt for a use case and role (runtime only)."""
    meta = get_use_case_meta(use_case)
    meta["system_prompt"][role] = prompt


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

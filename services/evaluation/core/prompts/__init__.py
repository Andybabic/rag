"""Prompt-Dateien unter ``core/prompts/`` (Katalog, Use-Case-Skripte, Agent-Suffix)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

_PROMPTS_DIR = Path(__file__).resolve().parent


class ActionDef(TypedDict):
    """Definition einer Agent-Aktion (wie in use_cases)."""

    enabled: bool
    description: str


def _read_json(name: str) -> dict:
    path = _PROMPTS_DIR / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _read_text(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


ACTION_CATALOG: dict[str, str] = _read_json("action_catalog.json")
ACTION_SIGNATURES: dict[str, str] = _read_json("action_signatures.json")
AGENT_REACT_SUFFIX: str = _read_text("agent_react_suffix.txt").rstrip() + "\n"


def _default_actions(*names: str) -> dict[str, ActionDef]:
    return {
        name: ActionDef(enabled=True, description=ACTION_CATALOG.get(name, ""))
        for name in names
    }


def _load_use_case_meta() -> dict[str, dict]:
    registry = _read_json("use_cases_registry.json")
    meta: dict[str, dict] = {}
    for uc_id, cfg in registry.items():
        roles: list[str] = cfg["roles"]
        system_prompt: dict[str, str] = {}
        for role in roles:
            path = _PROMPTS_DIR / "use_cases" / uc_id / f"{role}.txt"
            system_prompt[role] = path.read_text(encoding="utf-8").strip()
        meta[uc_id] = {
            "system_prompt": system_prompt,
            "agent_actions": _default_actions(*cfg["agent_action_names"]),
            "default_collection": cfg["default_collection"],
        }
    return meta


USE_CASE_META: dict[str, dict] = _load_use_case_meta()

"""Plugin registry – central point for use-case registration."""

from __future__ import annotations

import logging

from shared.usecase_config import get_use_case

from plugins.base import BasePlugin
from plugins.generic import GenericPlugin
from plugins.beispiel import BeispielPlugin
from plugins.gw_stpoelten import GWStPoeltenPlugin
from plugins.neumann import NeumannPlugin
from plugins.wiener_linien import WienerLinienPlugin
from plugins.ustp import UstpPlugin

PLUGIN_REGISTRY: dict[str, BasePlugin] = {}


def register_plugin(plugin: BasePlugin) -> None:
    """Register a plugin instance. Called once at import time."""
    PLUGIN_REGISTRY[plugin.use_case_id] = plugin


def get_plugin(use_case: str) -> BasePlugin:
    """Look up a plugin by use_case_id. Raises ValueError if unknown."""
    if use_case not in PLUGIN_REGISTRY:
        available = list(PLUGIN_REGISTRY.keys())
        raise ValueError(
            f"Unbekannter Use Case '{use_case}'. "
            f"Verfügbar: {available}"
        )
    return PLUGIN_REGISTRY[use_case]


async def resolve_plugin(use_case: str) -> BasePlugin | None:
    """Plugin für einen Use Case – eingebaut oder im Dashboard angelegt.

    Eingebaute Plugins haben Vorrang, sie tragen eigene Metadaten-Logik. Sonst
    wird der Use Case in der Datenbank nachgeschlagen und generisch verarbeitet.
    ``None`` heißt: weder eingebaut noch in der Datenbank (oder die Datenbank
    ist nicht erreichbar).
    """
    if use_case in PLUGIN_REGISTRY:
        return PLUGIN_REGISTRY[use_case]
    try:
        uc = await get_use_case(use_case)
    except Exception as exc:  # noqa: BLE001 — DB-Ausfall wie „unbekannt" behandeln
        logging.getLogger(__name__).warning(
            "Use-Case-Lookup für %r fehlgeschlagen: %s", use_case, exc
        )
        return None
    if uc is None:
        return None
    return GenericPlugin(uc.id, uc.default_collection or f"{uc.id}_default")


# ── Register all built-in plugins ────────────────────────────

register_plugin(NeumannPlugin())
register_plugin(GWStPoeltenPlugin())
register_plugin(WienerLinienPlugin())
register_plugin(BeispielPlugin())
register_plugin(UstpPlugin())

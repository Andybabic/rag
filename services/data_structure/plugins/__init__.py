"""Plugin registry – central point for use-case registration."""

from __future__ import annotations

from plugins.base import BasePlugin
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


# ── Register all built-in plugins ────────────────────────────

register_plugin(NeumannPlugin())
register_plugin(GWStPoeltenPlugin())
register_plugin(WienerLinienPlugin())
register_plugin(BeispielPlugin())
register_plugin(UstpPlugin())

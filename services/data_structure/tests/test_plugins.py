"""Tests for the plugin system."""

from __future__ import annotations

import pytest
from plugins import PLUGIN_REGISTRY, get_plugin
from plugins.base import BasePlugin
from plugins.beispiel import BeispielPlugin
from plugins.gw_stpoelten import GWStPoeltenPlugin
from plugins.neumann import NeumannPlugin
from plugins.wiener_linien import WienerLinienPlugin
from shared.models import ChunkMetadata


def _make_meta(**overrides) -> ChunkMetadata:
    defaults = {
        "file_name": "test.pdf",
        "doc_type": "pdf",
        "use_case": "test",
        "collection": "test_default",
    }
    return ChunkMetadata(**(defaults | overrides))


# ── Registry ─────────────────────────────────────────────────


def test_all_plugins_registered():
    assert "neumann" in PLUGIN_REGISTRY
    assert "gw_stpoelten" in PLUGIN_REGISTRY
    assert "wiener_linien" in PLUGIN_REGISTRY
    assert "beispiel" in PLUGIN_REGISTRY


def test_get_plugin_known():
    plugin = get_plugin("neumann")
    assert isinstance(plugin, NeumannPlugin)


def test_get_plugin_unknown_raises_valueerror():
    with pytest.raises(ValueError, match="Unbekannter Use Case 'xyz'"):
        get_plugin("xyz")


def test_get_plugin_unknown_lists_available():
    with pytest.raises(ValueError, match="neumann"):
        get_plugin("nonexistent")


def test_all_plugins_are_base_plugin_subclasses():
    for plugin in PLUGIN_REGISTRY.values():
        assert isinstance(plugin, BasePlugin)


def test_all_plugins_have_use_case_id():
    for name, plugin in PLUGIN_REGISTRY.items():
        assert plugin.use_case_id == name


# ── NeumannPlugin ────────────────────────────────────────────


def test_neumann_enrich_area_hydraulik():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Hydraulikdruck zu hoch", {"machine_id": "M-4711"})
    assert result.extra["area"] == "hydraulik"


def test_neumann_enrich_area_elektrik():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Spannung am Motor prüfen", {})
    assert result.extra["area"] == "elektrik"


def test_neumann_enrich_area_mechanik():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Lager austauschen", {})
    assert result.extra["area"] == "mechanik"


def test_neumann_enrich_area_default():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Allgemeine Information", {})
    assert result.extra["area"] == "allgemein"


def test_neumann_enrich_area_from_raw_meta():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Allgemeine Information", {"area": "hydraulik"})
    assert result.extra["area"] == "hydraulik"


def test_neumann_enrich_topic_stoerung():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Hydraulikdruck zu hoch – Störung", {})
    assert result.extra["topic"] == "stoerung"


def test_neumann_enrich_topic_wartung():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Wartung durchführen", {})
    assert result.extra["topic"] == "wartung"


def test_neumann_enrich_topic_sicherheit():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Sicherheit beachten", {})
    assert result.extra["topic"] == "sicherheit"


def test_neumann_enrich_topic_default():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(meta, "Allgemeine Information", {})
    assert result.extra["topic"] == "allgemein"


def test_neumann_enrich_combined():
    """'Hydraulikdruck zu hoch' → area=hydraulik, topic=stoerung (via 'Warnung')."""
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    result = plugin.enrich_metadata(
        meta, "Warnung: Hydraulikdruck zu hoch", {"machine_id": "M-4711"}
    )
    assert result.extra["area"] == "hydraulik"
    assert result.extra["topic"] == "stoerung"
    assert result.extra["machine_id"] == "M-4711"
    assert meta.collection == "neumann_m_4711"


def test_neumann_enrich_sets_collection_from_machine_id():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    plugin.enrich_metadata(meta, "text", {"machine_id": "M-4711"})
    assert meta.collection == "neumann_m_4711"


def test_neumann_enrich_no_machine_id_defaults_unknown():
    plugin = NeumannPlugin()
    meta = _make_meta(use_case="neumann")
    plugin.enrich_metadata(meta, "text", {})
    assert meta.collection == "neumann_unknown"
    assert meta.extra["machine_id"] == "unknown"


def test_neumann_collections():
    assert NeumannPlugin().get_collections() == ["neumann_machines"]


def test_neumann_system_prompt():
    prompt = NeumannPlugin().get_system_prompt()
    assert "Wartungsassistent" in prompt


# ── GWStPoeltenPlugin ────────────────────────────────────────


def test_gw_enrich_detects_cnc_block():
    """N10 G01 X100 F0.3 S1200 has >=3 tokens → CNC block."""
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(meta, "N10 G01 X100 F0.3 S1200", {})
    assert result.extra["is_cnc_block"] is True


def test_gw_enrich_cnc_cutting_speed():
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(meta, "N10 G01 X100 F0.3 S1200", {})
    assert result.extra["cutting_speed"] == 1200


def test_gw_enrich_cnc_operation_linearfraesen():
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(meta, "N10 G01 X100 F0.3 S1200", {})
    assert result.extra["operation_type"] == "Linearfraesen"


def test_gw_enrich_cnc_operation_kreisinterpolation():
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(meta, "N20 G02 X50 Y50 S800", {})
    assert result.extra["operation_type"] == "Kreisinterpolation"


def test_gw_enrich_cnc_operation_bohren():
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(meta, "N30 G81 X10 Z-5 S600", {})
    assert result.extra["operation_type"] == "Bohren"


def test_gw_enrich_cnc_collection():
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    plugin.enrich_metadata(meta, "N10 G01 X100 F0.3 S1200", {})
    assert meta.collection == "gw_cnc_steps"


def test_gw_enrich_cnc_raw_meta_fields():
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(
        meta,
        "N10 G01 X100 F0.3 S1200",
        {"tool_type": "Fräser", "material_class": "Stahl"},
    )
    assert result.extra["tool_type"] == "Fräser"
    assert result.extra["material_class"] == "Stahl"


def test_gw_enrich_no_cnc():
    """Normal text → not a CNC block, collection = gw_ruest_data."""
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(meta, "Fräskopf Typ A wechseln", {})
    assert result.extra["is_cnc_block"] is False
    assert meta.collection == "gw_ruest_data"


def test_gw_enrich_material_text():
    """Text with 'material' → collection = gw_material_info."""
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(meta, "Materialangabe Stahl", {})
    assert result.extra["is_cnc_block"] is False
    assert meta.collection == "gw_material_info"


def test_gw_enrich_product_and_ruest_ids():
    plugin = GWStPoeltenPlugin()
    meta = _make_meta(use_case="gw_stpoelten")
    result = plugin.enrich_metadata(
        meta, "text", {"product_id": "P-100", "ruest_map_id": "R-200"}
    )
    assert result.extra["product_id"] == "P-100"
    assert result.extra["ruest_map_id"] == "R-200"


def test_gw_collections():
    assert GWStPoeltenPlugin().get_collections() == [
        "gw_cnc_steps",
        "gw_ruest_data",
        "gw_material_info",
    ]


def test_gw_system_prompt():
    prompt = GWStPoeltenPlugin().get_system_prompt()
    assert "CNC-Rüstexperte" in prompt


def test_gw_agent_actions():
    actions = GWStPoeltenPlugin().get_agent_actions()
    assert "SEARCH_CNC" in actions
    assert "SEARCH" in actions
    assert "FINAL_ANSWER" in actions


# ── WienerLinienPlugin ───────────────────────────────────────


def test_wl_enrich_law_ref_and_criticality():
    """§ 34 Abs. 2 EisbG → law_ref set, criticality=high."""
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(
        meta, "Gemäß § 34 Abs. 2 EisbG ist die Verordnung bindend.", {}
    )
    assert result.extra["law_ref"] != ""
    assert "§ 34" in result.extra["law_ref"]
    assert result.extra["criticality"] == "high"


def test_wl_enrich_no_law_ref():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(meta, "Normaler Fließtext", {})
    assert result.extra["law_ref"] == ""
    assert result.extra["criticality"] == "medium"


def test_wl_collection_is_never_overridden():
    """Regression: topic-based collection routing previously sent §11
    Ersatzsignal (Fahrzeug-Inhalt) to wl_strecke because of the word
    "Signal". The plugin must NOT touch base.collection any more –
    everything goes through one wl_default collection."""
    plugin = WienerLinienPlugin()
    for text in (
        "Die Fahrzeugbremse muss geprüft werden.",
        "Gleis und Weiche inspizieren.",
        "Störung im Betrieb, Vorfall melden.",
        "Blinkt am Armaturenpult die weiße Drucktaste „Ersatzsignal\", so hat das Fahrpersonal …",
        "Prüfung: Aufgabe zum Lernziel.",
        "Allgemeine Information.",
    ):
        meta = _make_meta(use_case="wiener_linien", collection="wl_default")
        plugin.enrich_metadata(meta, text, {})
        assert meta.collection == "wl_default", f"unexpectedly rerouted: {text!r}"


def test_wl_topic_metadata_removed():
    """Legacy ``topic``/topic-based extras must not be set – they leak
    into citations and confuse consumers."""
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(meta, "Gleis und Weiche inspizieren.", {})
    assert "topic" not in result.extra


def test_wl_enrich_audience_default():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(meta, "text", {})
    assert result.extra["audience"] == "both"


def test_wl_enrich_audience_from_raw_meta():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(meta, "text", {"audience": "trainee"})
    assert result.extra["audience"] == "trainee"


def test_wl_difficulty_base():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(meta, "Kurzer Text.", {})
    assert result.extra["difficulty"] == 1


def test_wl_difficulty_long_text():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    text = "Wort " * 250  # >200 words → +1
    result = plugin.enrich_metadata(meta, text, {})
    assert result.extra["difficulty"] == 2


def test_wl_difficulty_very_long_text():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    text = "Wort " * 450  # >400 words → +1 +1
    result = plugin.enrich_metadata(meta, text, {})
    assert result.extra["difficulty"] == 3


def test_wl_difficulty_with_law_ref():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(meta, "Gemäß § 34 Abs. 2 EisbG.", {})
    assert result.extra["difficulty"] >= 2  # base 1 + law_ref 1


def test_wl_difficulty_with_technical_terms():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    result = plugin.enrich_metadata(meta, "Die Nennspannung beträgt 750V.", {})
    assert result.extra["difficulty"] >= 2  # base 1 + technical 1


def test_wl_difficulty_max_5():
    plugin = WienerLinienPlugin()
    meta = _make_meta(use_case="wiener_linien")
    # >400 words (+2) + law_ref (+1) + technical (+1) = 5
    text = "Wort " * 450 + " § 34 Abs. 2 EisbG Nennspannung"
    result = plugin.enrich_metadata(meta, text, {})
    assert result.extra["difficulty"] == 5


def test_wl_collections():
    # Single collection per use case after dropping topic-based routing.
    assert WienerLinienPlugin().get_collections() == ["wl_default"]


def test_wl_system_prompt_default():
    prompt = WienerLinienPlugin().get_system_prompt()
    assert "Fachpersonal" in prompt
    assert "§" in prompt


def test_wl_system_prompt_trainee():
    prompt = WienerLinienPlugin().get_system_prompt(role="trainee")
    assert "Auszubildende" in prompt
    assert "einfach" in prompt.lower()


def test_wl_system_prompt_different_roles():
    trainee = WienerLinienPlugin().get_system_prompt(role="trainee")
    expert = WienerLinienPlugin().get_system_prompt(role="expert")
    assert trainee != expert


def test_wl_agent_actions():
    actions = WienerLinienPlugin().get_agent_actions()
    assert "CLARIFY" in actions
    assert "SEARCH" in actions
    assert "FINAL_ANSWER" in actions


# ── BeispielPlugin ───────────────────────────────────────────


def test_beispiel_enrich_basic():
    plugin = BeispielPlugin()
    meta = _make_meta(use_case="beispiel")
    result = plugin.enrich_metadata(meta, "Normal text", {"kategorie": "test"})
    assert result.extra["kategorie"] == "test"
    assert result.extra["ist_wichtig"] is False


def test_beispiel_enrich_detects_warning():
    plugin = BeispielPlugin()
    meta = _make_meta(use_case="beispiel")
    result = plugin.enrich_metadata(meta, "ACHTUNG: Gefahr!", {})
    assert result.extra["ist_wichtig"] is True


def test_beispiel_enrich_default_kategorie():
    plugin = BeispielPlugin()
    meta = _make_meta(use_case="beispiel")
    result = plugin.enrich_metadata(meta, "text", {})
    assert result.extra["kategorie"] == "unbekannt"


def test_beispiel_collections():
    assert BeispielPlugin().get_collections() == ["beispiel_docs"]


def test_beispiel_system_prompt():
    prompt = BeispielPlugin().get_system_prompt()
    assert "Beispiel" in prompt


def test_beispiel_agent_actions():
    actions = BeispielPlugin().get_agent_actions()
    assert "SEARCH" in actions
    assert "FINAL_ANSWER" in actions

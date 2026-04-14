"""Tests for use case metadata."""

from __future__ import annotations

import pytest
from core.use_cases import get_agent_actions, get_default_collection, get_system_prompt


def test_neumann_system_prompt():
    prompt = get_system_prompt("neumann")
    assert "Wartungsassistent" in prompt


def test_gw_system_prompt():
    prompt = get_system_prompt("gw_stpoelten")
    assert "CNC-Rüstexperte" in prompt


def test_wl_system_prompt_default():
    prompt = get_system_prompt("wiener_linien")
    assert "Fachpersonal" in prompt


def test_wl_system_prompt_trainee():
    prompt = get_system_prompt("wiener_linien", role="trainee")
    assert "Auszubildende" in prompt


def test_wl_system_prompt_unknown_role_fallback():
    prompt = get_system_prompt("wiener_linien", role="unknown")
    assert "Fachpersonal" in prompt  # falls back to default


def test_neumann_actions():
    actions = get_agent_actions("neumann")
    assert "SEARCH" in actions
    assert "FINAL_ANSWER" in actions


def test_gw_actions_include_search_cnc():
    actions = get_agent_actions("gw_stpoelten")
    assert "SEARCH_CNC" in actions


def test_wl_actions_include_clarify():
    actions = get_agent_actions("wiener_linien")
    assert "CLARIFY" in actions


def test_default_collections():
    assert get_default_collection("neumann") == "neumann_machines"
    assert get_default_collection("gw_stpoelten") == "gw_cnc_steps"
    assert get_default_collection("wiener_linien") == "wl_fahrzeug"


def test_unknown_use_case():
    with pytest.raises(ValueError, match="Unbekannter Use Case"):
        get_system_prompt("nonexistent")

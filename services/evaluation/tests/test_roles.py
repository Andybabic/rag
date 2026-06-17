"""Tests für filter_actions – Use-Case-Such-Varianten erreichen Subagenten."""

from __future__ import annotations

from core.roles import get_role, filter_actions


def test_search_cnc_reaches_subagents():
    """SEARCH_CNC muss zu jeder Rolle durchkommen, die SEARCH darf –
    sonst kann der Manager-Pfad die CNC-Werkzeugsuche nie nutzen."""
    role = get_role("facts")
    actions = filter_actions(
        role,
        ["SEARCH", "SEARCH_CNC", "REFINE_QUERY", "RECALL_MEMORY", "FINAL_ANSWER"],
    )
    assert "SEARCH_CNC" in actions
    assert "SEARCH" in actions
    assert "FINAL_ANSWER" in actions


def test_no_search_variant_without_base_search():
    """Hat eine Rolle gar kein SEARCH (hypothetisch), wird auch keine
    SEARCH-Variante hinzugefügt."""
    role = get_role("facts")
    # Use case ohne SEARCH, nur SEARCH_CNC freigeschaltet
    actions = filter_actions(role, ["SEARCH_CNC", "FINAL_ANSWER"])
    assert "SEARCH_CNC" not in actions
    assert "FINAL_ANSWER" in actions


def test_final_answer_always_present():
    role = get_role("facts")
    assert "FINAL_ANSWER" in filter_actions(role, ["SEARCH"])


def test_disabled_actions_are_stripped():
    role = get_role("facts")
    # REFINE_QUERY nicht freigeschaltet -> nicht enthalten
    actions = filter_actions(role, ["SEARCH"])
    assert "REFINE_QUERY" not in actions

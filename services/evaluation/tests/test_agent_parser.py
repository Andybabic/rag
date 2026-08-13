"""Tests for the agent action parser."""

from __future__ import annotations

from core.agent import _parse_action


def test_parse_standard_action():
    response = (
        "THOUGHT: Ich suche nach Hydraulik\n"
        'ACTION: SEARCH({"query": "Hydraulik", "collection": "neumann_machines"})'
    )
    thought, action, args = _parse_action(response)
    assert thought == "Ich suche nach Hydraulik"
    assert action == "SEARCH"
    assert args["query"] == "Hydraulik"
    assert args["collection"] == "neumann_machines"


def test_parse_final_answer():
    response = (
        "THOUGHT: Ich habe genug Informationen\n"
        'ACTION: FINAL_ANSWER({"answer": "Der Druck beträgt 10 bar [1].", "extras": {}})'
    )
    thought, action, args = _parse_action(response)
    assert action == "FINAL_ANSWER"
    assert "10 bar" in args["answer"]


def test_parse_recall_memory():
    response = "THOUGHT: Ich prüfe den Kontext\nACTION: RECALL_MEMORY({})"
    thought, action, args = _parse_action(response)
    assert action == "RECALL_MEMORY"
    assert args == {}


def test_parse_clarify():
    response = (
        "THOUGHT: Die Frage ist unklar\n"
        'ACTION: CLARIFY({"question": "Welches Fahrzeug meinen Sie?"})'
    )
    thought, action, args = _parse_action(response)
    assert action == "CLARIFY"
    assert "Fahrzeug" in args["question"]


def test_parse_search_cnc():
    response = (
        "THOUGHT: CNC Code prüfen\n"
        'ACTION: SEARCH_CNC({"ruest_id": "4711", "missing_tool": "Fräskopf"})'
    )
    _, action, args = _parse_action(response)
    assert action == "SEARCH_CNC"
    assert args["ruest_id"] == "4711"


def test_parse_fallback_no_action():
    """When no ACTION pattern found, fallback to FINAL_ANSWER."""
    response = "Einfach eine Antwort ohne Format."
    thought, action, args = _parse_action(response)
    assert action == "FINAL_ANSWER"
    assert args["answer"] == response


def test_parse_thought_extraction():
    response = 'THOUGHT: Multi-word reasoning here\nACTION: SEARCH({"query": "test"})'
    thought, _, _ = _parse_action(response)
    assert thought == "Multi-word reasoning here"


def test_parse_empty_thought():
    response = 'ACTION: SEARCH({"query": "test"})'
    thought, action, _ = _parse_action(response)
    assert action == "SEARCH"


def test_parse_strips_thinking_before_action():
    response = (
        "<think>interne Überlegungen, nicht zeigen</think>\n"
        "THOUGHT: genug Infos\n"
        'ACTION: FINAL_ANSWER({"answer": "Hallo!", "extras": {}})'
    )
    thought, action, args = _parse_action(response)
    assert thought == "genug Infos"
    assert action == "FINAL_ANSWER"
    assert args["answer"] == "Hallo!"


def test_parse_nemotron_style_closing_tag():
    response = (
        "Here's a thinking process:\n1. greet\n"
        "</think>Hallo! Mir geht es gut."
    )
    _, action, args = _parse_action(response)
    assert action == "FINAL_ANSWER"
    assert args["answer"] == "Hallo! Mir geht es gut."

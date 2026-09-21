"""The two parsers that decide what runs and what is asked for first.

Both are pure functions, and both are load-bearing for safety rather than for
convenience: ``code_parser`` settles the permission flag that the execution
path later trusts, and ``call_parser`` decides which text in an answer is
treated as a request to execute code. Their edge cases are tested here because
neither failure mode is visible in ordinary output — a missed marker just runs
something quietly, and an over-eager match just executes something nobody
asked for.
"""

from __future__ import annotations

import pytest
from shared.skills import (
    SkillCodeError,
    detect_language,
    find_calls,
    parse_skill_code,
    substitute,
)

# ── code_parser: what exists and what needs consent ──────────────────────────


def test_python_functions_and_markers():
    source = """
import os

# Liefert das Wetter.
# allow-network: api.open-meteo.com, geocoding-api.open-meteo.com
def aktuell(ort, einheit="celsius"):
    return ort

# ask for permission
# Schreibt einen Eintrag.
def eintragen(text):
    return text

def _helper(x):
    return x
"""
    functions = {f.name: f for f in parse_skill_code(source, "python")}

    assert set(functions) == {"aktuell", "eintragen"}, "private helpers stay private"
    assert functions["aktuell"].params == ("ort", "einheit")
    assert functions["aktuell"].ask_for_permission is False
    assert functions["aktuell"].allowed_domains == (
        "api.open-meteo.com",
        "geocoding-api.open-meteo.com",
    )
    assert functions["eintragen"].ask_for_permission is True
    assert functions["eintragen"].allowed_domains == ()


def test_javascript_functions_and_markers():
    source = """
// ask for permission
// Sendet eine Nachricht.
export async function senden(empfaenger, text) { return 1 }

// allow-network: api.example.com
export const holen = async (id) => { return id }

function _intern() {}
"""
    functions = {f.name: f for f in parse_skill_code(source, "javascript")}

    assert set(functions) == {"senden", "holen"}
    assert functions["senden"].ask_for_permission is True
    assert functions["senden"].params == ("empfaenger", "text")
    assert functions["holen"].allowed_domains == ("api.example.com",)
    assert functions["holen"].ask_for_permission is False


def test_marker_does_not_leak_across_a_blank_line():
    """A comment block must abut its definition.

    If blank lines were skipped, the ``ask for permission`` above ``zwei``
    would also be read as belonging to ``eins`` — or worse, one function's
    trailing comment would grant consent to the next. The failure is silent in
    both directions, which is why it is pinned here.
    """
    source = """
def eins(a):
    return a

# ask for permission
def zwei(b):
    return b
"""
    functions = {f.name: f for f in parse_skill_code(source, "python")}
    assert functions["eins"].ask_for_permission is False
    assert functions["zwei"].ask_for_permission is True


def test_nested_definitions_are_not_entry_points():
    source = """
def aussen(x):
    def innen(y):
        return y
    return innen(x)
"""
    assert [f.name for f in parse_skill_code(source, "python")] == ["aussen"]


def test_url_in_allow_network_is_reduced_to_a_host():
    source = """
# allow-network: https://api.example.com/v1/data, not a domain
def f():
    return 1
"""
    assert parse_skill_code(source, "python")[0].allowed_domains == ("api.example.com",)


def test_file_without_callable_function_is_rejected():
    with pytest.raises(SkillCodeError):
        parse_skill_code("x = 1\n", "python")


def test_detect_language():
    assert detect_language("wetter.py") == "python"
    assert detect_language("wetter.js") == "javascript"
    with pytest.raises(SkillCodeError):
        detect_language("wetter.rb")


# ── call_parser: what counts as a call ───────────────────────────────────────


def test_finds_call_with_quoted_and_bare_arguments():
    text = 'Aktuell: wetter.aktuell("Nürnberg") und wetter.vorhersage(Berlin, 3).'
    calls = find_calls(text, known_skills={"wetter"})

    assert [(c.skill, c.function, c.args) for c in calls] == [
        ("wetter", "aktuell", ("Nürnberg",)),
        ("wetter", "vorhersage", ("Berlin", 3)),
    ]


def test_parentheses_inside_a_quoted_argument_do_not_close_the_call():
    text = 'wetter.aktuell("Frankfurt (Oder)")'
    (call,) = find_calls(text, known_skills={"wetter"})
    assert call.args == ("Frankfurt (Oder)",)


def test_unknown_skill_is_not_a_call():
    """Prose is full of ``word.word(...)``; only declared skills may match."""
    text = "Siehe config.json(x), str.format(y) und obj.a.b(1)."
    assert find_calls(text, known_skills={"wetter"}) == []


def test_property_chain_is_not_a_call():
    assert find_calls("data.wetter.aktuell(1)", known_skills={"wetter"}) == []


def test_unbalanced_parentheses_are_ignored():
    assert find_calls("wetter.aktuell(Nürnberg", known_skills={"wetter"}) == []


def test_backticked_call_is_consumed_whole():
    """A model that wraps the call in backticks must not leave them behind."""
    text = 'Wetter: `wetter.aktuell("Fürth")`.'
    (call,) = find_calls(text, known_skills={"wetter"})
    assert substitute(text, [(call, "14 °C")]) == "Wetter: 14 °C."


def test_substitution_is_right_to_left_so_offsets_stay_valid():
    text = "A: wetter.aktuell(X) B: wetter.aktuell(Y)"
    calls = find_calls(text, known_skills={"wetter"})
    result = substitute(text, [(calls[0], "eins"), (calls[1], "zwei")])
    assert result == "A: eins B: zwei"


def test_unreplaced_calls_stay_visible():
    """An unresolved call is left in the text rather than silently deleted."""
    text = "A: wetter.aktuell(X) B: wetter.unbekannt(Y)"
    calls = find_calls(text, known_skills={"wetter"})
    result = substitute(text, [(calls[0], "eins")])
    assert result == "A: eins B: wetter.unbekannt(Y)"


def test_call_count_is_capped():
    text = " ".join(f"wetter.f{i}(1)" for i in range(30))
    assert len(find_calls(text, known_skills={"wetter"})) <= 8


# ── mehrere Skills in einem Antworttext ──────────────────────────────────────


def test_calls_to_several_skills_are_all_found_in_order():
    text = (
        'Wetter: wetter.aktuell("Fürth"). '
        'Bestand: lager.bestand(M8x40). '
        'Brutto: rechner.brutto(100).'
    )
    calls = find_calls(text, known_skills={"wetter", "lager", "rechner"})

    assert [(c.skill, c.function) for c in calls] == [
        ("wetter", "aktuell"), ("lager", "bestand"), ("rechner", "brutto")
    ]


def test_only_the_use_cases_own_skills_match():
    """The filter is per use case: a skill this use case lacks stays text."""
    text = "wetter.aktuell(X) und fremd.funktion(Y)"
    calls = find_calls(text, known_skills={"wetter"})

    assert [(c.skill, c.function) for c in calls] == [("wetter", "aktuell")]


def test_same_function_name_in_two_skills_stays_distinct():
    """``aktuell`` may exist in several skills; the prefix is what disambiguates."""
    text = "wetter.aktuell(A) klima.aktuell(B)"
    calls = find_calls(text, known_skills={"wetter", "klima"})

    assert [c.skill for c in calls] == ["wetter", "klima"]
    assert {c.function for c in calls} == {"aktuell"}


def test_substitution_keeps_each_skills_result_in_its_own_place():
    text = "A: wetter.aktuell(X) B: lager.bestand(Y) C: rechner.brutto(1)"
    calls = find_calls(text, known_skills={"wetter", "lager", "rechner"})
    result = substitute(text, list(zip(calls, ["14 °C", "7 Stück", "1,19 EUR"])))

    assert result == "A: 14 °C B: 7 Stück C: 1,19 EUR"


def test_one_failed_skill_does_not_shift_the_others():
    """A call left unreplaced must not move its neighbours' substitutions."""
    text = "A: wetter.aktuell(X) B: lager.bestand(Y) C: rechner.brutto(1)"
    calls = find_calls(text, known_skills={"wetter", "lager", "rechner"})
    result = substitute(text, [(calls[0], "14 °C"), (calls[2], "1,19 EUR")])

    assert result == "A: 14 °C B: lager.bestand(Y) C: 1,19 EUR"

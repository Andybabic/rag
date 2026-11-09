"""Warn-code handling: codes named in a question are found in the hits or asked back."""

from core.actions import _mentions_any, _topics, _warn_codes
from core.agent import _clarify_question


def _hit(text: str, breadcrumb: str = "") -> dict:
    return {"metadata": {"text": text, "extra": {"breadcrumb": breadcrumb}}}


def test_finds_warn_and_fault_codes_in_question_and_anchor():
    assert _warn_codes("Warncode A30042 am FU", "Fehler F01590") == ["A30042", "F01590"]


def test_ignores_words_that_only_look_like_codes():
    assert _warn_codes("Parameter p0251 und Lüfter 2026") == []


def test_mentions_matches_whole_code_only():
    assert _mentions_any(_hit("Warnung A30042 ausgegeben"), ["A30042"])
    assert not _mentions_any(_hit("Warnung A300421 ausgegeben"), ["A30042"])


def test_topics_are_distinct_fault_breadcrumbs_in_rank_order():
    hits = [
        _hit("Warnung A30042 ausgegeben", "Störungen und Warnungen"),
        _hit("Störung F01590", "Störungen und Warnungen"),
        _hit("Legende der Symbole", "Legende"),
        _hit("Meldung: Geber", "Geber-Meldungen"),
    ]
    assert _topics(hits) == ["Störungen und Warnungen", "Geber-Meldungen"]


def test_clarify_question_names_the_code_and_lists_topics():
    question = _clarify_question(["A30042"], ["Geber", "Firmware"])
    assert "A30042" in question
    assert "- Geber" in question and "- Firmware" in question


def test_clarify_question_without_topics_still_asks():
    question = _clarify_question(["A30042"], [])
    assert "Komponente" in question and "Ähnliche Themen" not in question

"""Sub-agent fragments must not carry their own citation numbers.

Each sub-agent numbers its observations from 1, so [1] denotes a different
chunk in every fragment, and none of them line up with the global pool the
synthesizer cites against. Handed three conflicting numbering schemes, the
synthesizer produced a 1568-character answer with zero citations.
"""

from __future__ import annotations

from core.manager import _format_fragments


def _sub(role_label: str, answer: str, error: str | None = None) -> dict:
    return {
        "role_label": role_label,
        "sub_query": f"{role_label} query",
        "answer": answer,
        "error": error,
    }


def test_local_markers_are_removed():
    out = _format_fragments([_sub("Fakten", "Das Abgehen ist nicht nötig [4].")])

    assert "[4]" not in out
    assert "Das Abgehen ist nicht nötig." in out


def test_colliding_numbers_from_two_agents_both_disappear():
    out = _format_fragments([
        _sub("Prozedur", "Der Zug fährt ein [1]."),
        _sub("Fakten", "Aufschließen ist verboten [1]."),
    ])

    assert "[1]" not in out


def test_fragment_wording_survives_intact():
    """Only the markers go — dropping content would starve the synthesizer of
    the very material it is meant to merge."""
    out = _format_fragments([
        _sub("Prozedur", "Erst [1] Einfahrt, dann [2] Umrüsten, zuletzt [3] Ausfahrt.")
    ])

    assert "Erst Einfahrt, dann Umrüsten, zuletzt Ausfahrt." in out


def test_headers_are_preserved():
    out = _format_fragments([_sub("Kontext", "Hintergrund [2].")])

    assert "Kontext" in out
    assert "Kontext query" in out


def test_error_fragments_are_still_flagged():
    """A failed specialist must stay visible — the synthesizer has a rule for
    naming which aspect went unchecked."""
    out = _format_fragments([_sub("Fakten", "", error="Zeitlimit überschritten")])

    assert "Fehler: Zeitlimit überschritten" in out


def test_empty_fragment_is_marked_rather_than_blank():
    out = _format_fragments([_sub("Fakten", "")])

    assert "(leer)" in out

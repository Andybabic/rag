"""Neighbour context is cut at word boundaries, never inside a parameter name."""

from core.actions import _head, _tail, _with_context


def test_head_does_not_end_inside_a_parameter_name():
    text = "Abhilfe: Zähler auf 0 zurücksetzen (p0251 = 0) und Lüfter tauschen"
    cut = text.index("p0251") + 3  # would end in "p02"
    assert _head(text, cut) == "Abhilfe: Zähler auf 0 zurücksetzen"
    assert "p02" not in _head(text, cut)


def test_head_keeps_the_text_when_it_fits_or_ends_at_a_space():
    assert _head("kurz", 10) == "kurz"
    assert _head("ein Wort danach", 8) == "ein Wort"


def test_head_of_one_long_token_falls_back_to_a_plain_cut():
    assert _head("x" * 20, 5) == "xxxxx"


def test_tail_does_not_start_inside_a_word():
    assert _tail("alpha beta gamma", 8) == "gamma"
    assert _tail("alpha beta gamma", 6).strip() == "gamma"


def test_context_after_a_hit_keeps_the_value_that_follows():
    after = "Diese Meldung zeigt an: " + "x " * 120 + "Abhilfe: Zähler zurücksetzen (p0251 = 0)"
    framed = _with_context("A30042 Lüfter", "", after, reach=len(after) + 5)
    assert "(p0251 = 0)" in framed


def test_wider_reach_includes_more_than_the_default():
    after = "wort " * 200
    assert len(_with_context("x", "", after, reach=900)) > len(_with_context("x", "", after))

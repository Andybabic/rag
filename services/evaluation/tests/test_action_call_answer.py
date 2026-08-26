"""A malformed action call must not pass as a specialist's answer.

The ReAct parser falls back to FINAL_ANSWER whenever it cannot find a valid
action, so a broken SEARCH never surfaces as a parse error. It becomes the
sub-agent's answer, is marked sufficient, and reaches the synthesizer looking
like a contribution — while the specialist has in fact produced nothing. One
such agent is enough to mark a whole request sufficient.
"""

from __future__ import annotations

from core.agent import _is_action_call_answer

# Verbatim from a wiener_linien run: the whole answer of the "facts" sub-agent,
# which the pipeline reported as status=done, sufficient=True.
OBSERVED = (
    'SEARCH({"query": "Ausstiegsseite NT-Bediengeraet Konfiguration", '
    '"collection": "OBSERVATION", "filters": {}'
)


def test_the_observed_failure_is_caught():
    assert _is_action_call_answer(OBSERVED) is True


def test_other_action_verbs_and_bare_json():
    assert _is_action_call_answer('REFINE_QUERY({"query": "x"})') is True
    assert _is_action_call_answer('  FINAL_ANSWER({"answer": "y"})') is True
    assert _is_action_call_answer('{"action": "SEARCH", "args": {}}') is True


def test_real_answers_are_not_discarded():
    assert not _is_action_call_answer(
        "Die Ausstiegsseite wird durch Antippen der Pfeile geaendert [1]."
    )
    # An acronym opening a sentence — the reason the pattern demands the brace
    # of an argument object rather than just a parenthesis.
    assert not _is_action_call_answer("USTP (Use Case) beschreibt den Ablauf [2].")
    assert not _is_action_call_answer("NT-Geraet (Bediengeraet) laut Handbuch [3].")
    # A prose answer that merely mentions an action name is still an answer.
    assert not _is_action_call_answer(
        "Eine SEARCH-Anfrage liefert hier keine Treffer, weil der Begriff fehlt."
    )


def test_empty_and_none_are_not_action_calls():
    """Emptiness is already handled by the no-answer path; do not double-claim it."""
    assert _is_action_call_answer("") is False
    assert _is_action_call_answer(None) is False

"""A compliance verdict must not contradict the sources it was given.

The compliance model flagged as a hallucination a sentence standing verbatim in
the chunk it cited. It sees the full pool, so the text was in front of it. The
cost is not just the wasted call: a REWRITE runs a second synthesis in which
every citation number is reassigned, and one such pass has already moved a
correct statement onto an unrelated document's cover page.
"""

from __future__ import annotations

from core.manager import (
    _VERBATIM_RUN_WORDS,
    _drop_unfounded_issues,
    _longest_shared_run,
    _ungrounded_sentences,
    _words,
)

# Verbatim from ABU-BAs-087_03.pdf, S. 72. Note the chunk boundary cuts the
# last word: "Pfeil" where the answer says "Pfeile".
CHUNK = {
    "text": (
        "# Auswahl der Ausstiegsseite\n\n"
        "Die Ausstiegsseite der naechsten Station wird auf der Bedienoberflaeche "
        "durch einen aktiven linken bzw. rechten Pfeil neben der Haltestelle "
        "angezeigt. Das Fahrpersonal hat die Moeglichkeit, die Seite manuell "
        "durch Antippen der Pfeile zu aendern (notwendig im Gleiswechselbetrieb)."
        "\n\n# wird zu\n\n"
        "Aendert das Fahrpersonal manuell die Ausstiegsseite, aendert sich "
        "ebenfalls das Aussehen der Pfeil"
    ),
}

# The issue verbatim as the model produced it.
OBSERVED_ISSUE = (
    "Halluzination: Behauptung 'Aendert das Fahrpersonal manuell die "
    "Ausstiegsseite, aendert sich ebenfalls das Aussehen der Pfeile [1]' ist "
    "nicht im Chunk [1] belegt."
)

REAL_ISSUE = (
    "Halluzination: Die Aussage 'Der Zug faehrt anschliessend automatisch in "
    "die Wendeanlage und wird dort umgeruestet' steht in keinem Chunk."
)


def test_the_observed_false_positive_is_dropped():
    kept, dropped = _drop_unfounded_issues([OBSERVED_ISSUE], [CHUNK])
    assert kept == []
    assert dropped == [OBSERVED_ISSUE]


def test_a_genuine_hallucination_survives():
    kept, dropped = _drop_unfounded_issues([REAL_ISSUE], [CHUNK])
    assert kept == [REAL_ISSUE]
    assert dropped == []


def test_mixed_issues_are_separated():
    kept, dropped = _drop_unfounded_issues([OBSERVED_ISSUE, REAL_ISSUE], [CHUNK])
    assert kept == [REAL_ISSUE]
    assert dropped == [OBSERVED_ISSUE]


def test_chunk_boundary_mid_word_does_not_defeat_the_match():
    """The match has to survive 'Pfeile' vs the chunk's truncated 'Pfeil'."""
    claim = _words("aendert sich ebenfalls das Aussehen der Pfeile")
    pool = _words(CHUNK["text"])
    assert _longest_shared_run(claim, pool) >= 6  # everything up to the cut word


def test_a_short_quoted_phrase_cannot_clear_an_issue():
    """Common connective phrasing must not read as verbatim grounding."""
    short = "Halluzination: 'auf der Bedienoberflaeche' ist erfunden."
    kept, _ = _drop_unfounded_issues([short], [CHUNK])
    assert kept == [short]


def test_run_threshold_is_actually_enforced():
    pool = _words(CHUNK["text"])
    seven = _words("das Fahrpersonal hat die Moeglichkeit die Seite")
    assert len(seven) < _VERBATIM_RUN_WORDS
    assert _longest_shared_run(seven, pool) == len(seven)


def test_no_chunks_means_no_judgement():
    kept, dropped = _drop_unfounded_issues([OBSERVED_ISSUE], [])
    assert kept == [OBSERVED_ISSUE] and dropped == []


def test_unrelated_words_do_not_accumulate_a_run():
    assert _longest_shared_run(_words("a b c d e f g h"), _words(CHUNK["text"])) < 2


# The answer from the 12:01 run: three sentences, each near-verbatim from the
# chunk it cites. All three compliance issues below are about the same claim,
# and only the first one quotes it.
GROUNDED_ANSWER = (
    "Die Ausstiegsseite der naechsten Station wird auf der Bedienoberflaeche "
    "durch einen aktiven linken bzw. rechten Pfeil neben der Haltestelle "
    "angezeigt [1]. Das Fahrpersonal hat die Moeglichkeit, die Seite manuell "
    "durch Antippen der Pfeile zu aendern, was insbesondere im "
    "Gleiswechselbetrieb notwendig ist [1]. Aendert das Fahrpersonal manuell "
    "die Ausstiegsseite, aendert sich ebenfalls das Aussehen der Pfeile [1]."
)

RESTATEMENTS = [
    OBSERVED_ISSUE,
    "Quellenechtheit: Chunk [1] bestaetigt das manuelle Aendern via Antippen "
    "der Pfeile, aber nicht die visuelle Aenderung der Pfeile.",
    "Halluzination: Die Aussage ueber die visuelle Aenderung der Pfeile "
    "stammt nicht aus dem Pool.",
]


def test_restatements_without_a_quote_are_dropped_too():
    """The quote-based pass catches one of the three; the answer-side pass is
    what actually prevents the rewrite."""
    kept, dropped = _drop_unfounded_issues(RESTATEMENTS, [CHUNK], GROUNDED_ANSWER)
    assert kept == []
    assert len(dropped) == 3


def test_without_the_answer_the_restatements_survive():
    """Guards the ordering claim: the answer is what refutes them."""
    kept, _ = _drop_unfounded_issues(RESTATEMENTS, [CHUNK])
    assert len(kept) == 2


def test_a_grounded_answer_has_no_ungrounded_sentences():
    assert _ungrounded_sentences(GROUNDED_ANSWER, [CHUNK]) == []


def test_a_miscitation_stays_flagged():
    """From the 11:10 run: a true statement hung on an unrelated document.

    Groundedness is checked against the cited chunk, not the pool — otherwise
    this would be excused, since the content does exist somewhere in the pool.
    """
    cover_page = {"text": "Signalvorschrift fuer den Betrieb der U-Bahn mit "
                          "Stromschiene der Wiener Linien, Ausgabe 2024."}
    answer = (
        "Aendert das Fahrpersonal manuell die Ausstiegsseite, aendert sich "
        "ebenfalls das Aussehen der Pfeile [2]."
    )
    assert _ungrounded_sentences(answer, [CHUNK, cover_page])
    issue = ["Halluzination: Die Aussage stammt nicht aus dem Pool."]
    kept, dropped = _drop_unfounded_issues(issue, [CHUNK, cover_page], answer)
    assert kept == issue and dropped == []


def test_form_complaints_are_never_refuted_by_content():
    """A missing citation stays a missing citation however grounded the text."""
    issue = ["Citation-Coverage: Der zweite Satz traegt keine Quellennummer."]
    kept, _ = _drop_unfounded_issues(issue, [CHUNK], GROUNDED_ANSWER)
    assert kept == issue


def test_uncited_sentences_are_left_to_the_citation_check():
    """No [n] means nothing to verify against — not 'grounded'."""
    assert _ungrounded_sentences("Ein Satz voellig ohne Quellenangabe.", [CHUNK]) == []

"""What the agent sees per chunk decides whether it searches again.

The observation is the agent's entire view of a source. Cut it short and the
agent does not merely report less — it concludes the source is insufficient and
spends another ReAct round on material it already retrieved. That round costs
an LLM call plus a full retrieval; the text it was denied costs a few hundred
tokens. The budget therefore has to clear a whole chunk, not half of one.
"""

from __future__ import annotations

from core.actions import _OBSERVATION_CHARS
from core.manager import _POOL_CHUNK_CHARS

# Verbatim from ABU-BAs-087_03.pdf, S. 72 (500 characters) — a chunk of the
# size the chunker actually produces at DEFAULT_CHUNK_SIZE=256 tokens. Under
# the old 300-character budget the agent's view ended mid-procedure, before
# "durch Antippen der Pfeile" ever appeared.
EXIT_SIDE_CHUNK = (
    "[Wagentype X Triebwagen U-Bahn | Auswahl der Ausstiegsseite]\n\n"
    "# Auswahl der Ausstiegsseite\n\n"
    "Die Ausstiegsseite der naechsten Station wird auf der Bedienoberflaeche "
    "durch einen aktiven linken bzw. rechten Pfeil neben der Haltestelle "
    "angezeigt. Das Fahrpersonal hat die Moeglichkeit, die Seite manuell durch "
    "Antippen der Pfeile zu aendern (notwendig im Gleiswechselbetrieb).\n\n"
    "# wird zu\n\n"
    "Aendert das Fahrpersonal manuell die Ausstiegsseite, aendert sich "
    "ebenfalls das Aussehen der Pfeile."
)


def test_budget_clears_a_full_chunk():
    assert _OBSERVATION_CHARS >= len(EXIT_SIDE_CHUNK)


def test_the_procedure_survives_the_budget():
    """The old 300 cut before the one sentence that answers the question."""
    assert "durch Antippen der Pfeile" not in EXIT_SIDE_CHUNK[:300]
    assert "durch Antippen der Pfeile" in EXIT_SIDE_CHUNK[:_OBSERVATION_CHARS]


def test_budget_still_bounds_an_oversized_chunk():
    """Seven chunks share the agent's context; one wide table cannot own it."""
    assert len(("x" * 9000)[:_OBSERVATION_CHARS]) == _OBSERVATION_CHARS
    assert _OBSERVATION_CHARS < 2000


def test_agent_never_sees_more_than_the_synthesizer():
    """The agent decides; the synthesizer writes and must cite.

    An agent working from more text than the pool can show would assert things
    the synthesizer cannot back with a chunk, which surfaces as an answer whose
    citations do not cover it.
    """
    assert _OBSERVATION_CHARS <= _POOL_CHUNK_CHARS

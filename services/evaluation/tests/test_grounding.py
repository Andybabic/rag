"""Deterministic grounding: figures and names must come from the sources."""

from __future__ import annotations

from core.grounding import find_ungrounded

POOL = [
    "Welches Ereignis forderte mehr Verletzte: die Explosionen am verfluchten Dienstag?",
    "At half-time, Southgate brought on Luke Shaw for Kieran Trippier. Kane scored "
    "two goals in five games. More than 6,350 people have been injured.",
    "Pagers exploded, wounding some 2,800 across Lebanon. Hezbollah blamed Israel. "
    "Another strike east of Beirut killed at least six people. 1.2 million households.",
]


def test_invented_name_is_found():
    """Observed: 'Calvert-Lewin' cited to a chunk naming Palmer and Watkins."""
    found = find_ungrounded("Alternativen wie Calvert-Lewin kamen zu spät [4].", POOL)
    assert found["names"] == ["Calvert-Lewin"]


def test_invented_year_is_found():
    """Observed: the 2024 pager attacks dated to 2020 from model knowledge."""
    found = find_ungrounded("Die Explosionen am 4. August 2020 forderten 6.350 Verletzte [12].", POOL)
    assert found["numbers"] == ["2020"]


def test_backed_figures_in_other_notation_pass():
    answer = ("Bei den Pager-Explosionen gab es rund 2.800 Verletzte [3]; die "
              "Hisbollah-Führung beschuldigte Israel. 1,2 Millionen Haushalte waren betroffen.")
    assert find_ungrounded(answer, POOL) == {"numbers": [], "names": []}


def test_counts_list_markers_and_german_nouns_are_not_flagged():
    answer = ("1. Kane erzielte zwei Tore in fünf Spielen.\n2. Es gab 6 Tote östlich von "
              "Beirut [2]. Das libanesische Gesundheitsministerium meldete Verletzte.")
    assert find_ungrounded(answer, POOL) == {"numbers": [], "names": []}


def test_names_from_the_pool_pass():
    assert find_ungrounded("Southgate brachte Luke Shaw für Kieran Trippier.", POOL)["names"] == []


def test_german_word_pairs_are_not_names():
    """False alarms measured on a 100-question run."""
    answer = ("- **Weitere Angaben** fehlen. Folgende Prominente waren da. Beide Preise "
              "stimmen. Dritter Platz für Live-Daten aus Saudi-Arabien und der Ostsee-Region. "
              "Die Gruppe Freiwilliger half.")
    assert find_ungrounded(answer, POOL)["names"] == []


def test_real_invented_names_are_still_found():
    """Observed: three players named in no chunk."""
    answer = ("Southgate setzte auf Phil Foden statt auf James Maddison. "
              "- **Jarrod Bowen** kam zu spät.")
    assert set(find_ungrounded(answer, POOL)["names"]) == {"Phil Foden", "James Maddison", "Jarrod Bowen"}

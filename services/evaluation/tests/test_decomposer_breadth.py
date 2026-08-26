"""Retrieval breadth is (number of sub-tasks x step budget).

A plan that collapses an enumerative question into one sub-task caps the chunk
pool at what a single agent can find in three steps. Observed effect: the pool
fell from 20 chunks to 7 and the answer from 2328 to 548 characters.
"""

from __future__ import annotations

from core.manager import _DECOMPOSER_SYSTEM


def test_enumerative_questions_are_told_to_use_several_subtasks():
    assert "Aufzählende Fragen" in _DECOMPOSER_SYSTEM
    assert "2-3 Sub-Tasks" in _DECOMPOSER_SYSTEM


def test_the_single_subtask_rule_is_scoped_to_narrow_questions():
    """The rule used to read "Bei einer einfachen Faktenfrage reicht EIN
    Sub-Task" — broad enough that "Worauf ist zu achten?" qualified."""
    assert "eng umrissenen Einzelfrage" in _DECOMPOSER_SYSTEM
    assert "einfachen Faktenfrage" not in _DECOMPOSER_SYSTEM


def test_facets_are_named_so_the_split_is_not_arbitrary():
    """Without concrete facet examples the planner tends to emit near-duplicate
    sub-queries, which retrieve the same chunks twice instead of widening."""
    for facet in ("Ablauf", "Sicherheit", "Sonderfälle"):
        assert facet in _DECOMPOSER_SYSTEM


def test_the_three_subtask_cap_still_stands():
    assert "NIE mehr als 3 Sub-Tasks" in _DECOMPOSER_SYSTEM

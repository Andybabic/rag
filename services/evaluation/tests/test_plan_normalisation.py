"""Decomposer output validation — the plan decides what the answer can contain."""

from __future__ import annotations

from core.manager import _normalise_plan

QUERY = "Welche Angaben muss ein mündlicher Fahrauftrag beinhalten?"


def _plan(*roles: str) -> dict:
    # Sub-queries are numbered so they stay distinct: identical ones are now
    # deduplicated, and a fixture that repeats itself would be testing that
    # instead of what each case is about.
    return {
        "rationale": "test",
        "merge_strategy": "complementary",
        "subtasks": [
            {"role": r, "sub_query": f"{r} query {i}", "focus": ""}
            for i, r in enumerate(roles)
        ],
    }


def test_context_only_plan_gains_a_facts_subtask():
    """A context-only plan cannot answer the question: the context role's own
    prompt forbids it from returning concrete facts, so the synthesizer ends up
    with background prose and nothing citable."""
    result = _normalise_plan(_plan("context"), QUERY)

    roles = [st["role"] for st in result["subtasks"]]
    assert "facts" in roles
    assert "context" in roles, "the planner's own context task must survive"


def test_added_facts_subtask_asks_the_original_question():
    result = _normalise_plan(_plan("context"), QUERY)

    facts = next(st for st in result["subtasks"] if st["role"] == "facts")
    assert facts["sub_query"] == QUERY


def test_facts_contributor_is_placed_first():
    """Sub-agents run concurrently, but the fragment order reaches the
    synthesizer prompt — concrete content should lead."""
    result = _normalise_plan(_plan("context", "context"), QUERY)

    assert result["subtasks"][0]["role"] == "facts"


def test_subtask_cap_still_holds_after_the_insert():
    result = _normalise_plan(_plan("context", "context", "context"), QUERY)

    assert len(result["subtasks"]) == 3


def test_plan_with_a_facts_task_is_left_alone():
    result = _normalise_plan(_plan("facts", "context"), QUERY)

    assert [st["role"] for st in result["subtasks"]] == ["facts", "context"]


def test_identical_sub_queries_are_dropped():
    """Two sub-tasks asking the same thing cost twice and add nothing.

    Stripping placeholders can make previously distinct queries collide — in
    one run it turned a three-part plan into three copies of the user's own
    question, each running a full agent loop.
    """
    plan = {
        "rationale": "t", "merge_strategy": "complementary",
        "subtasks": [
            {"role": "facts", "sub_query": "Wo ist Gebäude A?", "focus": ""},
            {"role": "facts", "sub_query": "wo ist gebäude a?", "focus": ""},
            {"role": "context", "sub_query": "Wie komme ich dorthin?", "focus": ""},
        ],
    }
    queries = [st["sub_query"] for st in _normalise_plan(plan, QUERY)["subtasks"]]

    assert queries == ["Wo ist Gebäude A?", "Wie komme ich dorthin?"]


def test_a_sub_query_that_is_only_a_placeholder_is_dropped():
    """"Wetter in [Standort]?" is unanswerable — searching and tool-calling
    both have nothing to work with, and the specialist burns its whole step
    budget on it."""
    plan = {
        "rationale": "t", "merge_strategy": "complementary",
        "subtasks": [
            {"role": "facts", "sub_query": "Wetter in [Standort]?", "focus": ""},
            {"role": "facts", "sub_query": "Welche Buslinien fahren zur FH?", "focus": ""},
        ],
    }
    queries = [st["sub_query"] for st in _normalise_plan(plan, QUERY)["subtasks"]]

    assert queries == ["Welche Buslinien fahren zur FH?"]


def test_a_real_parenthetical_survives():
    """"(Oder)" and "(Niederösterreich)" disambiguate a place name — they are
    exactly what a good sub-query contains, not a placeholder."""
    plan = {
        "rationale": "t", "merge_strategy": "complementary",
        "subtasks": [
            {"role": "facts",
             "sub_query": "Wie ist das Wetter in Frankfurt (Oder)?", "focus": ""},
        ],
    }
    (st,) = _normalise_plan(plan, QUERY)["subtasks"]

    assert st["sub_query"] == "Wie ist das Wetter in Frankfurt (Oder)?"


def test_plan_with_a_procedure_task_is_left_alone():
    """Procedure retrieves concrete step content, so it already carries the
    answer — no facts task needs to be forced in alongside it."""
    result = _normalise_plan(_plan("procedure", "context"), QUERY)

    assert [st["role"] for st in result["subtasks"]] == ["procedure", "context"]

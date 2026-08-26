"""Decomposer output validation — the plan decides what the answer can contain."""

from __future__ import annotations

from core.manager import _normalise_plan

QUERY = "Welche Angaben muss ein mündlicher Fahrauftrag beinhalten?"


def _plan(*roles: str) -> dict:
    return {
        "rationale": "test",
        "merge_strategy": "complementary",
        "subtasks": [
            {"role": r, "sub_query": f"{r} query", "focus": ""} for r in roles
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


def test_plan_with_a_procedure_task_is_left_alone():
    """Procedure retrieves concrete step content, so it already carries the
    answer — no facts task needs to be forced in alongside it."""
    result = _normalise_plan(_plan("procedure", "context"), QUERY)

    assert [st["role"] for st in result["subtasks"]] == ["procedure", "context"]

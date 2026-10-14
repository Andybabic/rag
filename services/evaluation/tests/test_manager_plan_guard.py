"""The planner must not date events from its own knowledge (see manager)."""

from __future__ import annotations

from core.manager import _build_sub_system_prompt, _strip_invented_years
from core.roles import get_role


def _subs(*queries: str) -> dict:
    return {"subtasks": [{"sub_query": q} for q in queries]}


def _stripped(question: str, *queries: str) -> list[str]:
    return [st["sub_query"] for st in _strip_invented_years(_subs(*queries), question)["subtasks"]]


def test_invented_parenthetical_is_dropped_whole():
    # Observed: "verfluchter Dienstag" (2024 pager attacks) became Beirut 2020.
    assert _stripped(
        "Explosionen am „verfluchten Dienstag“?",
        "Verletzte Explosionen ‚verfluchter Dienstag‘ (Beirut 2020)",
    ) == ["Verletzte Explosionen ‚verfluchter Dienstag‘"]


def test_bare_invented_years_go_with_their_connector():
    assert _stripped(
        "Taylor Swifts London-Konzerte?",
        "Besucherzahl Taylor Swift London 2023 oder 2024",
        "Opferzahlen 2023/2024",
    ) == ["Besucherzahl Taylor Swift London", "Opferzahlen"]


def test_years_from_the_question_stay():
    assert _stripped(
        "Wer gewann die EM 2024?", "Sieger der Fußball-EM 2024", "Finale EM 2024 (Berlin, 2024)"
    ) == ["Sieger der Fußball-EM 2024", "Finale EM 2024 (Berlin, 2024)"]


def test_unchanged_plan_untouched():
    assert _stripped("Wie hoch ist der Druck?", "Betriebsdruck Hydraulik") == ["Betriebsdruck Hydraulik"]


def test_sub_agent_sees_the_users_question():
    prompt = _build_sub_system_prompt("BASE", get_role("facts"), "Fokus", "Was war mehr, A oder B?")
    assert "GESAMTFRAGE DES NUTZERS" in prompt
    assert "Was war mehr, A oder B?" in prompt
    assert "GESAMTFRAGE" not in _build_sub_system_prompt("BASE", get_role("facts"), "Fokus")


def test_single_facet_keeps_the_users_question():
    from core.manager import _keep_single_question
    q = "Welches Ereignis führte trotz der Leistung von Harry Kane zur Kritik an Southgate?"
    plan = {"subtasks": [{"sub_query": "Kritik an Southgate, obwohl Kane bester Spieler war",
                          "focus": "f"}]}
    assert _keep_single_question(plan, q, None)["subtasks"][0]["sub_query"] == q


def test_rewrite_kept_for_follow_ups_and_several_facets():
    from core.manager import _keep_single_question
    q = "Und beim Modell 2699 der Firma Neumann, wie hoch ist dort der Druck?"
    one = {"subtasks": [{"sub_query": "Betriebsdruck Modell 2699", "focus": ""}]}
    history = [{"role": "user", "content": "Druck Modell 2600?"}]
    assert _keep_single_question(one, q, history)["subtasks"][0]["sub_query"] == "Betriebsdruck Modell 2699"
    two = {"subtasks": [{"sub_query": "A", "focus": ""}, {"sub_query": "B", "focus": ""}]}
    assert [s["sub_query"] for s in _keep_single_question(two, q, None)["subtasks"]] == ["A", "B"]


def test_answer_form_is_validated():
    from core.manager import _normalise_plan
    base = {"subtasks": [{"role": "facts", "sub_query": "Welches Ereignis?", "focus": ""}]}
    assert _normalise_plan({**base, "answer_form": "single"}, "q")["answer_form"] == "single"
    assert _normalise_plan({**base, "answer_form": "essay"}, "q")["answer_form"] is None


def test_answer_form_inferred_from_the_question():
    from core.manager import infer_answer_form
    assert infer_answer_form("Welches Ereignis forderte mehr Verletzte: A oder B?") == "comparison"
    assert infer_answer_form("Wer hat einen höheren Weltrang, Navarro oder Jovic?") == "comparison"
    assert infer_answer_form("Welches Ereignis führte zur Kritik an Southgate?") == "single"
    assert infer_answer_form("Wie viele Vereine?") == "single"
    assert infer_answer_form("Welche Spieler besiegten Alcaraz?") == "list"
    assert infer_answer_form("Wie funktioniert die Bremsprüfung?") == "explanation"

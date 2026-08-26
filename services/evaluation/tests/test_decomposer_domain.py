"""The decomposer's sub-queries are what hit the index, so they must be
phrased in the corpus' vocabulary — not in general-knowledge terms."""

from __future__ import annotations

import json

import pytest

import core.manager as manager

DOMAIN = (
    "Du beantwortest Fragen zum U-Bahn-Betrieb der Wiener Linien "
    "auf Basis der Signalvorschrift-U-Bahn (SV-U)."
)


@pytest.fixture
def captured(monkeypatch):
    """Capture the messages the decomposer would send to the LLM."""
    seen: dict = {}

    async def fake_call_llm(messages, **kwargs):
        seen["messages"] = messages
        return json.dumps({
            "rationale": "r",
            "merge_strategy": "complementary",
            "subtasks": [
                {"role": "facts", "sub_query": "Ersatzsignal", "focus": "f"}
            ],
        })

    monkeypatch.setattr(manager, "call_llm", fake_call_llm)
    return seen


@pytest.mark.anyio
async def test_domain_prompt_reaches_the_decomposer(captured):
    await manager.plan_subtasks(
        "Wie ist die Fahrt mit Ersatzsignal fortzusetzen?",
        use_case="wl", history=None, domain_prompt=DOMAIN,
    )

    system = captured["messages"][0]["content"]
    assert "Signalvorschrift-U-Bahn" in system


@pytest.mark.anyio
async def test_decomposer_is_told_to_keep_the_domain_vocabulary(captured):
    """Without this the planner substitutes its own domain: an Ersatzsignal
    question came back as sub-queries about road junctions, which retrieve
    nothing and cost every sub-agent an extra REFINE_QUERY round."""
    await manager.plan_subtasks(
        "Wie ist die Fahrt mit Ersatzsignal fortzusetzen?",
        use_case="wl", history=None, domain_prompt=DOMAIN,
    )

    system = captured["messages"][0]["content"]
    assert "FACHBEGRIFFE" in system
    assert "unverändert" in system


@pytest.mark.anyio
async def test_vocabulary_rule_does_not_invite_copying_the_question(captured):
    """The first wording of this rule ("Terminologie ... wörtlich in die
    sub_query") made the planner emit the user's question verbatim as a single
    sub-task. Retrieval breadth is sub-tasks x step budget, so that halved the
    chunk pool and the answer shrank from 2300 to 550 characters."""
    await manager.plan_subtasks(
        "Worauf ist nach dem Abstellen des Zuges zu achten?",
        use_case="wl", history=None, domain_prompt=DOMAIN,
    )

    system = captured["messages"][0]["content"]
    assert "keine Zerlegung" in system
    assert "eigene Facette" in system


@pytest.mark.anyio
async def test_domain_brief_is_truncated(captured):
    await manager.plan_subtasks(
        "Frage", use_case="wl", history=None, domain_prompt="x" * 9000,
    )

    system = captured["messages"][0]["content"]
    assert "x" * manager._DOMAIN_BRIEF_CHARS in system
    assert "x" * (manager._DOMAIN_BRIEF_CHARS + 1) not in system


@pytest.mark.anyio
async def test_no_domain_block_when_prompt_is_blank(captured):
    await manager.plan_subtasks(
        "Frage", use_case="wl", history=None, domain_prompt="   ",
    )

    system = captured["messages"][0]["content"]
    assert system == manager._DECOMPOSER_SYSTEM


@pytest.mark.anyio
async def test_plan_still_parses_with_the_domain_block(captured):
    plan = await manager.plan_subtasks(
        "Frage", use_case="wl", history=None, domain_prompt=DOMAIN,
    )

    assert [st["role"] for st in plan["subtasks"]] == ["facts"]

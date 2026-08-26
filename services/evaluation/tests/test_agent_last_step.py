"""The final ReAct step must be spent answering, not searching.

An agent that searches on its last step exits with no answer, and the fallback
then dumps raw chunks under a local [1..n] numbering. As a manager fragment
that dump is actively harmful — the synthesizer cannot map those numbers onto
the global pool and drops citations from the final answer entirely.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.agent import run_agent

LAST_STEP_MARKER = "LETZTER SCHRITT"

# run_agent mutates one message list in place, so recording call.args[0] would
# hand every assertion the same final-state object. Snapshot per call instead.
_SENT: list[list[dict]] = []


def _always_search():
    async def _call(messages, **kwargs):
        _SENT.append([dict(m) for m in messages])
        return 'THOUGHT: Ich suche weiter\nACTION: SEARCH({"query": "test"})'

    return AsyncMock(side_effect=_call)


def _mock_search():
    return AsyncMock(return_value="[1] (Score: 0.92) Betriebsdruck max 10 bar...")


async def _run(mock_llm, max_steps=3):
    _SENT.clear()
    await run_agent(
        query="Wie erfolgt eine Wendefahrt?",
        use_case="neumann",
        session_id="last-step-session",
        system_prompt="Test prompt",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        collection="neumann_machines",
        max_steps=max_steps,
        persist_memory=False,
    )
    return list(_SENT)


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search)
@patch("core.agent.call_llm", new_callable=_always_search)
async def test_final_step_demands_an_answer(mock_llm, mock_search):
    conversations = await _run(mock_llm)

    last_turn = conversations[-1][-1]
    assert last_turn["role"] == "user"
    assert LAST_STEP_MARKER in last_turn["content"]


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search)
@patch("core.agent.call_llm", new_callable=_always_search)
async def test_earlier_steps_are_left_free_to_search(mock_llm, mock_search):
    """Forcing the answer too early would waste the step budget the role was
    given for refining its retrieval."""
    conversations = await _run(mock_llm)

    for turn in conversations[0] + conversations[1]:
        assert LAST_STEP_MARKER not in turn["content"]


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search)
@patch("core.agent.call_llm", new_callable=_always_search)
async def test_prompt_is_added_exactly_once(mock_llm, mock_search):
    """It is appended to a running message list, so a repeated append would
    stack duplicates into the final call."""
    conversations = await _run(mock_llm)

    hits = [t for t in conversations[-1] if LAST_STEP_MARKER in t["content"]]
    assert len(hits) == 1


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search)
@patch("core.agent.call_llm", new_callable=_always_search)
async def test_single_step_budget_still_searches_first(mock_llm, mock_search):
    """With max_steps=1 nothing has been retrieved yet, so demanding an answer
    would produce one with no grounding at all."""
    conversations = await _run(mock_llm, max_steps=1)

    for turn in conversations[0]:
        assert LAST_STEP_MARKER not in turn["content"]

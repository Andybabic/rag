"""Tests for the ReAct agent core with mocked external calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from core.agent import run_agent
from core.memory import clear_memory_store, read_memory


@pytest.fixture(autouse=True)
def _clean_memory():
    clear_memory_store()
    yield
    clear_memory_store()


def _search_then_answer():
    """Mock LLM: first call returns SEARCH, second returns FINAL_ANSWER."""
    responses = [
        'THOUGHT: Ich suche nach relevanten Informationen\n'
        'ACTION: SEARCH({"query": "Hydraulikdruck", "collection": "neumann_machines"})',
        'THOUGHT: Ich habe genug Informationen\n'
        'ACTION: FINAL_ANSWER({"answer": "Der Betriebsdruck beträgt 10 bar [1].", "extras": {}})',
    ]
    return AsyncMock(side_effect=responses)


def _immediate_answer():
    """Mock LLM: returns FINAL_ANSWER immediately."""
    return AsyncMock(
        return_value=(
            'THOUGHT: Die Antwort ist direkt klar\n'
            'ACTION: FINAL_ANSWER({"answer": "Die Lösung ist einfach.", "extras": {}})'
        )
    )


def _no_final_answer():
    """Mock LLM: never returns FINAL_ANSWER (exceeds max_steps)."""
    return AsyncMock(
        return_value=(
            'THOUGHT: Ich suche weiter\n'
            'ACTION: SEARCH({"query": "test"})'
        )
    )


def _mock_search_observation():
    """Mock action_search to return a formatted observation."""
    return AsyncMock(return_value="[1] (Score: 0.92) Betriebsdruck max 10 bar...")


# ── Basic agent flow ─────────────────────────────────────────


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search_observation)
@patch("core.agent.call_llm", new_callable=_search_then_answer)
async def test_agent_search_then_final_answer(mock_llm, mock_search):
    result = await run_agent(
        query="Wie hoch ist der Hydraulikdruck?",
        use_case="neumann",
        session_id="test-session",
        system_prompt="Du bist ein Wartungsassistent.",
        available_actions=["SEARCH", "RECALL_MEMORY", "LOOKUP_SOURCES", "FINAL_ANSWER"],
        collection="neumann_machines",
        max_steps=3,
    )

    assert result["answer"] == "Der Betriebsdruck beträgt 10 bar [1]."
    assert result["sufficient"] is True
    assert result["use_case"] == "neumann"
    assert result["session_id"] == "test-session"
    assert len(result["agent_steps"]) == 2

    # Step 1: SEARCH
    assert result["agent_steps"][0]["action"] == "SEARCH"
    assert result["agent_steps"][0]["step"] == 1

    # Step 2: FINAL_ANSWER
    assert result["agent_steps"][1]["action"] == "FINAL_ANSWER"
    assert result["agent_steps"][1]["step"] == 2


@pytest.mark.anyio
@patch("core.agent.call_llm", new_callable=_immediate_answer)
async def test_agent_immediate_answer(mock_llm):
    result = await run_agent(
        query="Einfache Frage",
        use_case="neumann",
        session_id="test-session",
        system_prompt="Test prompt",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        max_steps=3,
    )

    assert result["sufficient"] is True
    assert len(result["agent_steps"]) == 1
    assert result["agent_steps"][0]["action"] == "FINAL_ANSWER"


# ── Max steps reached ────────────────────────────────────────


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search_observation)
@patch("core.agent.call_llm", new_callable=_no_final_answer)
async def test_agent_max_steps_no_exception(mock_llm, mock_search):
    result = await run_agent(
        query="Schwierige Frage",
        use_case="neumann",
        session_id="test-session",
        system_prompt="Test prompt",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        collection="neumann_machines",
        max_steps=2,
    )

    assert result["sufficient"] is False
    assert "präzisieren" in result["answer"]
    assert len(result["agent_steps"]) == 2


# ── Memory update ─────────────────────────────────────────────


@pytest.mark.anyio
@patch("core.agent.call_llm", new_callable=_immediate_answer)
async def test_agent_memory_updated(mock_llm):
    await run_agent(
        query="Testfrage",
        use_case="neumann",
        session_id="mem-session",
        system_prompt="Test prompt",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        max_steps=3,
    )

    # Give the async task a moment to complete
    import asyncio
    await asyncio.sleep(0.1)

    memory = await read_memory("mem-session")
    assert "Testfrage" in memory


# ── All use cases ─────────────────────────────────────────────


@pytest.mark.anyio
@patch("core.agent.call_llm", new_callable=_immediate_answer)
async def test_agent_neumann(mock_llm):
    result = await run_agent(
        query="Störung Hydraulik",
        use_case="neumann",
        session_id="s1",
        system_prompt="Wartungsassistent",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        max_steps=3,
    )
    assert result["use_case"] == "neumann"
    assert result["sufficient"] is True


@pytest.mark.anyio
@patch("core.agent.call_llm", new_callable=_immediate_answer)
async def test_agent_gw_stpoelten(mock_llm):
    result = await run_agent(
        query="Fräskopf Alternative",
        use_case="gw_stpoelten",
        session_id="s2",
        system_prompt="CNC Experte",
        available_actions=["SEARCH", "SEARCH_CNC", "FINAL_ANSWER"],
        max_steps=3,
    )
    assert result["use_case"] == "gw_stpoelten"
    assert result["sufficient"] is True


@pytest.mark.anyio
@patch("core.agent.call_llm", new_callable=_immediate_answer)
async def test_agent_wiener_linien(mock_llm):
    result = await run_agent(
        query="Bremsprüfung Vorschrift",
        use_case="wiener_linien",
        session_id="s3",
        system_prompt="WL Assistent",
        available_actions=["SEARCH", "CLARIFY", "FINAL_ANSWER"],
        max_steps=3,
    )
    assert result["use_case"] == "wiener_linien"
    assert result["sufficient"] is True


# ── Agent steps content ───────────────────────────────────────


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search_observation)
@patch("core.agent.call_llm", new_callable=_search_then_answer)
async def test_agent_steps_contain_all_fields(mock_llm, mock_search):
    result = await run_agent(
        query="Test",
        use_case="neumann",
        session_id="s1",
        system_prompt="Test",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        collection="neumann_machines",
        max_steps=3,
    )

    for step in result["agent_steps"]:
        assert "step" in step
        assert "thought" in step
        assert "action" in step
        assert "args" in step
        assert "observation" in step

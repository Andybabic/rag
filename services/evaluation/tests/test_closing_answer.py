"""A loop that runs out of steps still has to produce a real answer.

Asking for FINAL_ANSWER inside the loop is only a prompt, and the model ignores
it often enough to matter. Without a real answer the agent falls back to
dumping raw chunks under a local [1..n] numbering — which, as a manager
fragment, the synthesizer cannot map onto the global pool, and it responds by
dropping citations from the final answer entirely.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.agent import _closing_answer, run_agent

MESSAGES = [
    {"role": "system", "content": "prompt"},
    {"role": "user", "content": "Wie erfolgt eine Wendefahrt?"},
    {"role": "user", "content": "OBSERVATION: [1] (abu.pdf, S. 82) Der Zug wendet."},
]


def _llm(reply: str):
    return AsyncMock(return_value=reply)


@pytest.mark.anyio
async def test_plain_prose_reply_is_used():
    with patch("core.agent.call_llm", _llm("Der Zug wendet in der Anlage [1].")):
        answer, sufficient = await _closing_answer(MESSAGES, use_case="wl")

    assert answer == "Der Zug wendet in der Anlage [1]."
    assert sufficient is True


@pytest.mark.anyio
async def test_react_wrapped_reply_is_unwrapped():
    """The model often stays in ReAct format out of habit; the answer argument
    is what belongs in the fragment, not the JSON envelope."""
    reply = (
        'THOUGHT: Ich habe genug\n'
        'ACTION: FINAL_ANSWER({"answer": "Der Zug wendet [1].", "extras": {}})'
    )
    with patch("core.agent.call_llm", _llm(reply)):
        answer, _ = await _closing_answer(MESSAGES, use_case="wl")

    assert answer == "Der Zug wendet [1]."


@pytest.mark.anyio
async def test_prose_with_a_stray_thought_line_is_kept_whole():
    """_parse_action falls back to FINAL_ANSWER carrying the entire response
    when it finds no ACTION, so the reply arrives intact here. Stripping the
    framework artefacts is run_agent's job — see the end-to-end test below."""
    with patch("core.agent.call_llm", _llm("THOUGHT: kurz\nDer Zug wendet [1].")):
        answer, _ = await _closing_answer(MESSAGES, use_case="wl")

    assert "Der Zug wendet [1]." in answer


@pytest.mark.anyio
async def test_failed_call_falls_through_to_the_caller():
    """Signalling "no answer" lets run_agent use its raw-chunk fallback rather
    than returning an empty fragment."""
    failing = AsyncMock(side_effect=RuntimeError("LLM down"))
    with patch("core.agent.call_llm", failing):
        answer, sufficient = await _closing_answer(MESSAGES, use_case="wl")

    assert answer == ""
    assert sufficient is False


@pytest.mark.anyio
async def test_empty_reply_falls_through_to_the_caller():
    with patch("core.agent.call_llm", _llm("   \n  ")):
        answer, sufficient = await _closing_answer(MESSAGES, use_case="wl")

    assert answer == ""
    assert sufficient is False


@pytest.mark.anyio
async def test_observations_are_carried_into_the_closing_call():
    """It must answer from what was already retrieved — a closing call without
    the observations would have nothing to ground in and would hallucinate."""
    spy = _llm("Antwort [1].")
    with patch("core.agent.call_llm", spy):
        await _closing_answer(MESSAGES, use_case="wl")

    sent = spy.call_args.args[0]
    assert any("OBSERVATION" in m["content"] for m in sent)
    assert sent[-1]["content"].startswith("Der Schrittvorrat ist aufgebraucht")


# ── End-to-end through the loop ──────────────────────────────

# run_agent only collects chunks from a dict result (agent.py: "Handle
# structured results"), and the closing call is gated on having chunks — a
# bare observation string would skip the path under test entirely.
SEARCH_RESULT = {
    "observation": "[1] (abu.pdf, S. 82) Der Zug wendet in der Anlage.",
    "chunks": [{
        "text": "Der Zug wendet in der Anlage.",
        "score": 0.9,
        "metadata": {"file_name": "abu.pdf", "page": 82},
    }],
    "searched_collections": ["neumann_machines"],
}


def _mock_search_with_chunks():
    return AsyncMock(return_value=SEARCH_RESULT)


def _search_forever_then_answer():
    """Never emits FINAL_ANSWER during the loop; answers only when the closing
    call demands it — exactly the observed failure mode."""
    return AsyncMock(side_effect=[
        'THOUGHT: suchen\nACTION: SEARCH({"query": "a"})',
        'THOUGHT: suchen\nACTION: SEARCH({"query": "b"})',
        'THOUGHT: suchen\nACTION: SEARCH({"query": "c"})',
        "Die Wendefahrt erfolgt in drei Phasen [1].",
    ])


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search_with_chunks)
@patch("core.agent.call_llm", new_callable=_search_forever_then_answer)
async def test_exhausted_loop_returns_a_real_answer(mock_llm, mock_search):
    result = await run_agent(
        query="Wie erfolgt eine Wendefahrt?",
        use_case="neumann",
        session_id="closing-session",
        system_prompt="Test prompt",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        collection="neumann_machines",
        max_steps=3,
        persist_memory=False,
    )

    assert result["answer"] == "Die Wendefahrt erfolgt in drei Phasen [1]."
    assert "Basierend auf den gefundenen Dokumenten" not in result["answer"]


@pytest.mark.anyio
@patch("core.agent.action_search", new_callable=_mock_search_with_chunks)
@patch("core.agent.call_llm", new_callable=lambda: AsyncMock(side_effect=[
    'THOUGHT: suchen\nACTION: SEARCH({"query": "a"})',
    'THOUGHT: suchen\nACTION: SEARCH({"query": "b"})',
    'THOUGHT: suchen\nACTION: SEARCH({"query": "c"})',
    "THOUGHT: genug gesucht\nDie Wendefahrt erfolgt in drei Phasen [1].",
]))
async def test_framework_artefacts_are_stripped_on_the_way_out(mock_llm, mock_search):
    """A closing reply that still carries a THOUGHT line must not surface it."""
    result = await run_agent(
        query="Wie erfolgt eine Wendefahrt?",
        use_case="neumann",
        session_id="closing-session-2",
        system_prompt="Test prompt",
        available_actions=["SEARCH", "FINAL_ANSWER"],
        collection="neumann_machines",
        max_steps=3,
        persist_memory=False,
    )

    assert "THOUGHT" not in result["answer"]
    assert "Die Wendefahrt erfolgt in drei Phasen [1]." in result["answer"]

"""The answer must not wait on diagnostics.

similarity_to_rank_1 and answer_similarity are read in one place — the Eval
page, from the database. Nothing in the chat view shows them. Computing them
means embedding every chunk plus the answer, measured at 5.3 s of a 28.4 s
request. Blocking the response on that spends a fifth of the user's wait on
numbers nobody is waiting for, and lets a slow embedding service delay an
answer that is already finished.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import core.manager as manager
from core.manager import _chunk_metrics_by_text, run_manager


def _chunk(text, score=0.5):
    return {"text": text, "score": score, "metadata": {"file_name": "a.pdf", "page": 1}}


@pytest.fixture
def pipeline(monkeypatch):
    """Stub every collaborator of run_manager; keep the orchestration real."""
    chunks = [_chunk("chunk one", 0.9), _chunk("chunk two", 0.4)]

    async def fake_run_agent(*a, **kw):
        return {
            "answer": "Fragment",
            "agent_steps": [{"step": 1, "action": "SEARCH", "chunks": chunks}],
            "sufficient": True,
            "searched_collections": ["c"],
        }

    async def fake_plan(*a, **kw):
        return {"merge_strategy": "complementary", "rationale": "r",
                "subtasks": [{"role": "facts", "sub_query": "q", "focus": "f"}]}

    async def fake_synth(*a, **kw):
        return "Eine belegte Aussage [1].", kw.get("subagents") and chunks or chunks

    async def fake_compliance(*a, **kw):
        return {"verdict": "OK", "guidance": "", "issues": []}

    monkeypatch.setattr(manager, "run_agent", fake_run_agent)
    monkeypatch.setattr(manager, "plan_subtasks", fake_plan)
    monkeypatch.setattr(manager, "synthesize", fake_synth)
    monkeypatch.setattr(manager, "check_compliance", fake_compliance)
    monkeypatch.setattr(manager, "recall", AsyncMock(return_value=[]))
    monkeypatch.setattr(manager, "remember", AsyncMock(return_value=None))
    return chunks


async def _run(events, **over):
    async def on_event(e):
        events.append(e)

    return await run_manager(
        "Wie?", use_case="wiener_linien", session_id="s", use_case_prompt="p",
        available_actions=["SEARCH", "FINAL_ANSWER"], on_event=on_event, **over
    )


@pytest.mark.anyio
async def test_answer_is_sent_before_the_embedding_call(pipeline):
    """The ordering *is* the feature: enrichment after 'final', not before."""
    order: list[str] = []

    async def slow_enrich(subagents, global_chunks, answer):
        order.append("enrich")
        for c in global_chunks:
            c["similarity_to_rank_1"] = 0.5
            c["answer_similarity"] = 0.25

    events: list[dict] = []
    with patch.object(manager, "_enrich_chunk_similarities", slow_enrich):
        async def on_event(e):
            events.append(e)
            if e["type"] == "final":
                order.append("final")
        await run_manager(
            "Wie?", use_case="wiener_linien", session_id="s", use_case_prompt="p",
            available_actions=["SEARCH"], on_event=on_event,
        )

    assert order == ["final", "enrich"]


@pytest.mark.anyio
async def test_returned_result_still_carries_the_metrics(pipeline):
    """The caller writes this to the DB, which is what the Eval page reads."""
    async def enrich(subagents, global_chunks, answer):
        for c in global_chunks:
            c["similarity_to_rank_1"] = 0.77
            c["answer_similarity"] = 0.33

    events: list[dict] = []
    with patch.object(manager, "_enrich_chunk_similarities", enrich):
        result = await _run(events)

    assert all(c["similarity_to_rank_1"] == 0.77 for c in result["global_chunks"])
    assert result["audit"]["post_response_ms"] >= 0


@pytest.mark.anyio
async def test_a_failing_embedding_service_no_longer_costs_the_answer(pipeline):
    """Before the reorder this exception happened while the user was waiting."""
    async def boom(*a, **kw):
        raise RuntimeError("embedding service down")

    events: list[dict] = []
    with patch.object(manager, "_enrich_chunk_similarities", boom):
        result = await _run(events)

    assert result["answer"] == "Eine belegte Aussage [1]."
    assert [e["type"] for e in events].count("final") == 1


@pytest.mark.anyio
async def test_metrics_event_follows_the_answer(pipeline):
    async def enrich(subagents, global_chunks, answer):
        for c in global_chunks:
            c["similarity_to_rank_1"] = 0.9
            c["answer_similarity"] = 0.1

    events: list[dict] = []
    with patch.object(manager, "_enrich_chunk_similarities", enrich):
        await _run(events)

    types = [e["type"] for e in events]
    assert types.index("final") < types.index("chunk_metrics")


def test_metrics_are_keyed_the_way_the_client_can_match_them():
    """Keyed on the text prefix, not position: the same chunk appears in the
    sub-agent steps and in the global pool, and neither side enumerates them
    identically."""
    c = _chunk("x" * 400)
    c["similarity_to_rank_1"], c["answer_similarity"] = 0.8, 0.2
    out = _chunk_metrics_by_text([{"agent_steps": [{"chunks": [c]}]}], [c])
    assert list(out) == ["x" * 200]
    assert out["x" * 200] == {"similarity_to_rank_1": 0.8, "answer_similarity": 0.2}


def test_unenriched_chunks_are_omitted():
    """A chunk the enrichment skipped must not be reported as measured."""
    assert _chunk_metrics_by_text([], [_chunk("no metrics here")]) == {}

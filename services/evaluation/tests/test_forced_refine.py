"""Die erzwungene Zweitsuche hängt am Suchergebnis, nicht am Wortlaut.

Gemessen im Betrieb: Ein Spezialist hatte den Messerkopf samt Werkzeug-Nr.
gefunden und schloss mit „dazu enthalten die Dokumente keine Information zur
Verfügbarkeit" – eine belegte Aussage. Die Auto-Korrektur sah „keine
Information", erzwang eine breitere Suche und bekam dieselben Treffer zurück.
Kosten: 10,4 s pro Vorkommen, ohne jeden Gewinn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from core.agent import run_agent
from core.memory import clear_memory_store

_NO_INFO = (
    'THOUGHT: Die Chunks decken die Verfügbarkeit nicht ab\n'
    'ACTION: FINAL_ANSWER({"answer": "Der Messerkopf Dm63 ist in Bauteil '
    '11221.5277 dokumentiert [1]. Dazu enthalten die Dokumente keine '
    'Information zur aktuellen Verfügbarkeit.", "extras": {}})'
)
_SEARCH = (
    'THOUGHT: Ich suche in den Rüstunterlagen\n'
    'ACTION: SEARCH({"query": "Messerkopf Dm63", "collection": "gw_cnc_steps"})'
)


@pytest.fixture(autouse=True)
def _clean_memory():
    clear_memory_store()
    yield
    clear_memory_store()


def _search_result(chunks):
    return AsyncMock(return_value={
        "observation": "[1] MESSERKOPF Ø63 mm – Werkzeug-Nr. T12",
        "chunks": chunks,
        "searched_collections": ["gw_cnc_steps"],
    })


def _chunk():
    return {"text": "MESSERKOPF Ø63 mm, Werkzeug-Nr. T12", "score": 0.9,
            "metadata": {"extra": {"product_id": "11221.5277"}}}


async def _run(max_steps=4):
    return await run_agent(
        query="11221.5277 Neuer Mitarbeiter findet den Dm63 Messerkopf nicht",
        use_case="gw_stpoelten",
        session_id="test-session",
        system_prompt="Du bist ein CNC-Rüstexperte.",
        available_actions=["SEARCH", "REFINE_QUERY", "FINAL_ANSWER"],
        collection="gw_cnc_steps",
        max_steps=max_steps,
    )


@pytest.mark.anyio
async def test_grounded_no_info_answer_is_accepted():
    """Mit Treffern in der Hand ist „dazu steht nichts drin" eine Antwort."""
    llm = AsyncMock(side_effect=[_SEARCH, _NO_INFO])
    with patch("core.agent.call_llm", llm), \
            patch("core.agent.action_search", _search_result([_chunk()])):
        result = await _run()

    actions = [s["action"] for s in result["agent_steps"]]
    assert actions == ["SEARCH", "FINAL_ANSWER"]
    assert "REFINE_QUERY" not in actions


@pytest.mark.anyio
async def test_empty_handed_no_info_answer_still_triggers_one_retry():
    """Ohne jeden Treffer bleibt die Zweitsuche die richtige Notbremse."""
    final = (
        'THOUGHT: Auch die breitere Suche bringt nichts\n'
        'ACTION: FINAL_ANSWER({"answer": "Dazu liegen keine Dokumente vor.", '
        '"extras": {}})'
    )
    llm = AsyncMock(side_effect=[_SEARCH, _NO_INFO, final])
    refine = AsyncMock(return_value={
        "observation": "[Verfeinerte Suche] Keine Treffer.",
        "chunks": [],
        "searched_collections": ["gw_cnc_steps"],
    })
    with patch("core.agent.call_llm", llm), \
            patch("core.agent.action_search", _search_result([])), \
            patch("core.agent.action_refine_query", refine):
        result = await _run()

    actions = [s["action"] for s in result["agent_steps"]]
    assert actions == ["SEARCH", "REFINE_QUERY", "FINAL_ANSWER"]
    assert refine.await_count == 1

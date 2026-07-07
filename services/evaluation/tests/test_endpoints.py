"""Tests for evaluation service endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from core.memory import clear_memory_store
from main import app


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _chunks(scores: list[float]) -> list[dict]:
    return [
        {"chunk_id": f"id-{i}", "text": f"chunk text {i}", "score": s, "metadata": {}}
        for i, s in enumerate(scores)
    ]


# ── POST /v1/rerank ─────────────────────────────────────────


@pytest.mark.anyio
async def test_rerank_sorts_correctly(client):
    resp = await client.post("/v1/rerank", json={
        "query": "test query",
        "chunks": _chunks([0.3, 0.9, 0.5]),
        "use_case": "neumann",
    })
    assert resp.status_code == 200
    body = resp.json()
    scores = [c["rerank_score"] for c in body["reranked"]]
    assert scores == sorted(scores, reverse=True)
    assert body["total"] == 3


@pytest.mark.anyio
async def test_rerank_threshold(client):
    resp = await client.post("/v1/rerank", json={
        "query": "test",
        "chunks": _chunks([0.1, 0.5, 0.8]),
        "use_case": "neumann",
        "config": {"threshold": 0.4},
    })
    body = resp.json()
    assert body["threshold_passed"] is True
    assert len(body["reranked"]) == 2
    assert len(body["below_threshold"]) == 1


@pytest.mark.anyio
async def test_rerank_all_below_threshold(client):
    resp = await client.post("/v1/rerank", json={
        "query": "test",
        "chunks": _chunks([0.1, 0.2]),
        "use_case": "neumann",
        "config": {"threshold": 0.5},
    })
    body = resp.json()
    assert body["threshold_passed"] is False
    assert len(body["reranked"]) == 0


@pytest.mark.anyio
async def test_rerank_has_request_id(client):
    resp = await client.post("/v1/rerank", json={
        "query": "test",
        "chunks": _chunks([0.5]),
        "use_case": "neumann",
    })
    assert "request_id" in resp.json()


# ── POST /v1/evaluate ───────────────────────────────────────


@pytest.mark.anyio
async def test_evaluate_sufficient(client):
    resp = await client.post("/v1/evaluate", json={
        "query": "Hydraulikdruck",
        "chunks": _chunks([0.8, 0.6, 0.7]),
        "use_case": "neumann",
        "min_chunks": 2,
        "min_score": 0.5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["sufficient"] is True
    assert body["recommended_action"] == "proceed"


@pytest.mark.anyio
async def test_evaluate_insufficient(client):
    resp = await client.post("/v1/evaluate", json={
        "query": "Hydraulikdruck",
        "chunks": _chunks([0.8, 0.3, 0.2]),
        "use_case": "neumann",
        "min_chunks": 2,
        "min_score": 0.5,
    })
    body = resp.json()
    assert body["sufficient"] is False
    assert body["recommended_action"] == "refine_query"
    assert body["refined_query_hint"] is not None


# ── POST /v1/citations ──────────────────────────────────────


def _citation_chunks(n: int) -> list[dict]:
    return [
        {
            "chunk_id": f"id-{i}",
            "text": f"Source content for chunk {i}.",
            "metadata": {"file_name": f"doc_{i}.pdf", "page": i + 1},
        }
        for i in range(n)
    ]


@pytest.mark.anyio
async def test_citations_maps_refs(client):
    resp = await client.post("/v1/citations", json={
        "answer": "Laut [1] und [2] ist der Wert korrekt.",
        "chunks": _citation_chunks(3),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["citations"]) == 2
    assert body["citations"][0]["ref"] == "[1]"
    assert body["citations"][0]["file_name"] == "doc_0.pdf"
    assert body["citations"][0]["page"] == 1
    assert body["citations"][1]["ref"] == "[2]"


@pytest.mark.anyio
async def test_citations_maps_three_refs(client):
    resp = await client.post("/v1/citations", json={
        "answer": "Siehe [1], [2] und [3].",
        "chunks": _citation_chunks(3),
    })
    body = resp.json()
    assert len(body["citations"]) == 3
    refs = [c["ref"] for c in body["citations"]]
    assert refs == ["[1]", "[2]", "[3]"]


@pytest.mark.anyio
async def test_citations_no_refs(client):
    resp = await client.post("/v1/citations", json={
        "answer": "Keine Referenzen.",
        "chunks": _citation_chunks(3),
    })
    body = resp.json()
    assert body["citations"] == []


# ── POST /v1/agent/query ─────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_memory():
    clear_memory_store()
    yield
    clear_memory_store()


def _agent_body(use_case: str = "neumann", **overrides):
    body = {
        "query": "Wie hoch ist der Hydraulikdruck?",
        "use_case": use_case,
        "session_id": "test-session",
        "role": "default",
        "config": {"collection": "neumann_machines", "max_steps": 3},
    }
    body.update(overrides)
    return body


def _mock_llm_search_then_answer():
    responses = [
        'THOUGHT: Suche relevante Daten\n'
        'ACTION: SEARCH({"query": "Hydraulikdruck", "collection": "neumann_machines"})',
        'THOUGHT: Antwort gefunden\n'
        'ACTION: FINAL_ANSWER({"answer": "Der Druck beträgt 10 bar.", "extras": {}})',
    ]
    return AsyncMock(side_effect=responses)


def _mock_llm_immediate():
    return AsyncMock(
        return_value=(
            'THOUGHT: Antwort klar\n'
            'ACTION: FINAL_ANSWER({"answer": "Einfache Antwort.", "extras": {}})'
        )
    )


def _mock_llm_no_answer():
    return AsyncMock(
        return_value='THOUGHT: Suche weiter\nACTION: SEARCH({"query": "test"})'
    )


@pytest.mark.anyio
@patch(
    "core.agent.action_search",
    new_callable=lambda: AsyncMock(return_value="[1] Score 0.92: Relevante Daten"),
)
@patch("core.agent.call_llm", new_callable=_mock_llm_search_then_answer)
async def test_agent_query_basic(mock_llm, mock_search, client):
    resp = await client.post("/v1/agent/query", json=_agent_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Der Druck beträgt 10 bar."
    assert body["sufficient"] is True
    assert body["use_case"] == "neumann"
    assert body["session_id"] == "test-session"
    assert "request_id" in body
    assert len(body["agent_steps"]) == 2


@pytest.mark.anyio
@patch(
    "core.agent.action_search",
    new_callable=lambda: AsyncMock(return_value="[1] Score 0.92: Relevante Daten"),
)
@patch("core.agent.call_llm", new_callable=_mock_llm_search_then_answer)
async def test_agent_steps_content(mock_llm, mock_search, client):
    resp = await client.post("/v1/agent/query", json=_agent_body())
    body = resp.json()
    steps = body["agent_steps"]

    assert steps[0]["action"] == "SEARCH"
    assert steps[0]["step"] == 1
    assert "thought" in steps[0]
    assert "observation" in steps[0]

    assert steps[1]["action"] == "FINAL_ANSWER"
    assert steps[1]["step"] == 2


@pytest.mark.anyio
@patch(
    "core.agent.action_search",
    new_callable=lambda: AsyncMock(return_value="Keine Ergebnisse"),
)
@patch("core.agent.call_llm", new_callable=_mock_llm_no_answer)
async def test_agent_max_steps_graceful(mock_llm, mock_search, client):
    resp = await client.post(
        "/v1/agent/query",
        json=_agent_body(config={"collection": "neumann_machines", "max_steps": 2}),
    )
    body = resp.json()
    assert body["sufficient"] is False
    assert "präzisieren" in body["answer"]
    assert len(body["agent_steps"]) == 2


@pytest.mark.anyio
async def test_agent_unknown_use_case(client):
    resp = await client.post("/v1/agent/query", json=_agent_body(use_case="unknown"))
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown_use_case"


@pytest.mark.anyio
@patch("core.agent.call_llm", new_callable=_mock_llm_immediate)
async def test_agent_gw_stpoelten(mock_llm, client):
    resp = await client.post("/v1/agent/query", json=_agent_body(
        use_case="gw_stpoelten",
        query="Fräskopf Alternative für Rüstungs-ID 4711",
        config={"collection": "gw_cnc_steps", "max_steps": 3},
    ))
    assert resp.status_code == 200
    assert resp.json()["use_case"] == "gw_stpoelten"


@pytest.mark.anyio
@patch("core.agent.call_llm", new_callable=_mock_llm_immediate)
async def test_agent_wiener_linien(mock_llm, client):
    resp = await client.post("/v1/agent/query", json=_agent_body(
        use_case="wiener_linien",
        query="Bremsprüfung Vorschrift",
        config={"collection": "wl_default", "max_steps": 3},
    ))
    assert resp.status_code == 200
    assert resp.json()["use_case"] == "wiener_linien"


# ── POST /v1/log ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_log_positive_feedback(client):
    pool = AsyncMock()
    with patch("router.v1.get_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/v1/log", json={
            "query_id": "11111111-1111-1111-1111-111111111111",
            "feedback": "positive",
            "comment": "Sehr hilfreich",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["feedback"] == "positive"
    pool.execute.assert_awaited()


@pytest.mark.anyio
async def test_log_negative_feedback(client):
    pool = AsyncMock()
    with patch("router.v1.get_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/v1/log", json={
            "query_id": "11111111-1111-1111-1111-111111111111",
            "feedback": "negative",
        })
    assert resp.status_code == 200
    assert resp.json()["feedback"] == "negative"


@pytest.mark.anyio
async def test_log_invalid_feedback(client):
    resp = await client.post("/v1/log", json={
        "query_id": "test-query-id",
        "feedback": "maybe",
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_feedback"

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


class _FakeReranker:
    """Scores each chunk by the number in its text – deterministic, no model."""

    def predict(self, pairs, show_progress_bar=False):
        return [float(text.rsplit(" ", 1)[-1]) for _, text in pairs]


@pytest.mark.anyio
async def test_rerank_sorts_correctly(client, monkeypatch):
    # Without a loaded model the endpoint keeps the input order on purpose
    # (fallback); sorting is only defined for real reranker scores.
    import core.reranker as reranker_mod

    monkeypatch.setattr(reranker_mod, "_RERANKER_ENABLED", True)
    monkeypatch.setattr(reranker_mod, "_model", _FakeReranker())
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
async def test_rerank_threshold(client, monkeypatch):
    # Fallback path on purpose (scores passed through), so the result does not
    # depend on whether this machine has a reranker configured.
    import core.reranker as reranker_mod

    monkeypatch.setattr(reranker_mod, "_model", None)
    monkeypatch.setattr(reranker_mod, "_model_load_failed", True)
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


def _manager_result(**overrides) -> dict:
    """A canned run_manager result. The agent loop itself is covered in
    test_agent.py; these tests check what the endpoint does around it."""
    result = {
        "answer": "Der Druck beträgt 10 bar [1].",
        "citations": [{"ref": 1, "file_name": "handbuch.pdf"}],
        "agent_steps": [
            {"step": 1, "action": "MANAGER_PLAN", "thought": "", "observation": ""},
            {"step": 2, "action": "SEARCH", "thought": "Suche", "observation": "[1] ..."},
            {"step": 3, "action": "FINAL_ANSWER", "thought": "", "observation": ""},
        ],
        "sufficient": True,
        "session_id": "test-session",
        "use_case": "neumann",
        "global_chunks": [],
        "searched_collections": ["neumann_machines"],
    }
    result.update(overrides)
    return result


@pytest.fixture()
def no_db():
    """Keep endpoint tests from writing query rows into a real database."""
    with patch("router.v1.get_pool", new=AsyncMock(side_effect=RuntimeError("no db"))):
        yield


@pytest.mark.anyio
async def test_agent_query_basic(client, no_db):
    manager = AsyncMock(return_value=_manager_result())
    with patch("router.v1.run_manager", new=manager):
        resp = await client.post("/v1/agent/query", json=_agent_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Der Druck beträgt 10 bar [1]."
    assert body["sufficient"] is True
    assert body["use_case"] == "neumann"
    assert body["session_id"] == "test-session"
    assert "request_id" in body
    kwargs = manager.await_args.kwargs
    assert kwargs["query"] == "Wie hoch ist der Hydraulikdruck?"
    assert kwargs["collection"] == "neumann_machines"


@pytest.mark.anyio
async def test_agent_steps_content(client, no_db):
    with patch("router.v1.run_manager", new=AsyncMock(return_value=_manager_result())):
        resp = await client.post("/v1/agent/query", json=_agent_body())
    steps = resp.json()["agent_steps"]
    assert [s["action"] for s in steps] == ["MANAGER_PLAN", "SEARCH", "FINAL_ANSWER"]
    assert all("thought" in s and "observation" in s for s in steps)


@pytest.mark.anyio
async def test_agent_insufficient_result_passed_through(client, no_db):
    """An unanswerable query comes back as a normal 200 with sufficient=False –
    the endpoint must not turn it into an error or a confident answer."""
    result = _manager_result(
        answer="Zu dieser Anfrage wurden keine passenden Dokumente gefunden.",
        sufficient=False,
        citations=[],
    )
    with patch("router.v1.run_manager", new=AsyncMock(return_value=result)):
        resp = await client.post(
            "/v1/agent/query",
            json=_agent_body(config={"collection": "neumann_machines", "max_steps": 2}),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sufficient"] is False
    assert "keine passenden Dokumente" in body["answer"]


@pytest.mark.anyio
async def test_agent_unknown_use_case(client):
    resp = await client.post("/v1/agent/query", json=_agent_body(use_case="unknown"))
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown_use_case"


@pytest.mark.anyio
async def test_agent_gw_stpoelten(client, no_db):
    manager = AsyncMock(return_value=_manager_result(use_case="gw_stpoelten"))
    with patch("router.v1.run_manager", new=manager):
        resp = await client.post("/v1/agent/query", json=_agent_body(
            use_case="gw_stpoelten",
            query="Fräskopf Alternative für Rüstungs-ID 4711",
            config={"collection": "gw_cnc_steps", "max_steps": 3},
        ))
    assert resp.status_code == 200
    assert resp.json()["use_case"] == "gw_stpoelten"
    assert manager.await_args.kwargs["use_case"] == "gw_stpoelten"


@pytest.mark.anyio
async def test_agent_wiener_linien(client, no_db):
    manager = AsyncMock(return_value=_manager_result(use_case="wiener_linien"))
    with patch("router.v1.run_manager", new=manager):
        resp = await client.post("/v1/agent/query", json=_agent_body(
            use_case="wiener_linien",
            query="Bremsprüfung Vorschrift",
            config={"collection": "wl_default", "max_steps": 3},
        ))
    assert resp.status_code == 200
    assert resp.json()["use_case"] == "wiener_linien"
    assert manager.await_args.kwargs["use_case"] == "wiener_linien"


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


@pytest.mark.anyio
async def test_ingest_log_delete_by_hash_returns_removed_rows(client):
    from unittest.mock import AsyncMock, MagicMock, patch

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{
        "file_name": "11221.7199 (Chiron 243) – Einstellblatt 1.pdf",
        "file_hash": "abc", "stored_path": None, "use_case": "gw_stpoelten",
    }])
    with patch("router.v1.get_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/v1/ingest-log/delete", json={"file_hashes": ["abc", "def"]})
    assert resp.status_code == 200
    [doc] = resp.json()["documents"]
    assert doc == {
        "file_name": "11221.7199 (Chiron 243) – Einstellblatt 1.pdf",
        "file_hash": "abc", "stored_path": "", "use_case": "gw_stpoelten",
    }
    assert pool.fetch.await_args.args[1] == ["abc", "def"]

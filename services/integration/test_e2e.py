"""End-to-end integration tests for all three use cases.

Runs as a Docker container that calls every service in sequence.
Usage: docker compose -f docker-compose.dev.yml run integration-test
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# ── Helpers ───────────────────────────────────────────────────────


def _url(service: str) -> str:
    mapping = {
        "cleaning": os.getenv(
            "CLEANING_SERVICE_URL", "http://cleaning:8001"
        ),
        "data_structure": os.getenv(
            "DATA_STRUCTURE_SERVICE_URL", "http://data_structure:8002"
        ),
        "embedding": os.getenv(
            "EMBEDDING_SERVICE_URL", "http://embedding:8003"
        ),
        "vectordb": os.getenv(
            "VECTORDB_SERVICE_URL", "http://vectordb:8004"
        ),
        "evaluation": os.getenv(
            "EVALUATION_SERVICE_URL", "http://evaluation:8005"
        ),
        "frontend": os.getenv(
            "FRONTEND_URL", "http://frontend:3000"
        ),
    }
    return mapping[service]


TIMEOUT = httpx.Timeout(120.0, connect=30.0)


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


def _ingest(client: httpx.Client, file_path: Path, use_case: str) -> dict:
    """Run the full ingestion pipeline for a file."""
    # 1. Clean
    with open(file_path, "rb") as f:
        clean_resp = client.post(
            f"{_url('cleaning')}/v1/clean",
            files={"file": (file_path.name, f)},
        )
    assert clean_resp.status_code == 200, (
        f"Clean failed: {clean_resp.text}"
    )
    clean_data = clean_resp.json()
    assert clean_data["markdown"], "Cleaned markdown is empty"

    # 2. Structure
    structure_resp = client.post(
        f"{_url('data_structure')}/v1/structure",
        json={
            "markdown": clean_data["markdown"],
            "metadata": clean_data.get("metadata", {}),
            "use_case": use_case,
        },
    )
    assert structure_resp.status_code == 200, (
        f"Structure failed: {structure_resp.text}"
    )
    structure_data = structure_resp.json()
    chunks = structure_data["chunks"]
    assert len(chunks) > 0, "No chunks produced"

    # 3. Embed
    embed_items = [
        {"type": "text", "content": c["text"], "metadata": {}}
        for c in chunks
    ]
    embed_resp = client.post(
        f"{_url('embedding')}/v1/embed/batch",
        json={"chunks": embed_items},
    )
    assert embed_resp.status_code == 200, (
        f"Embed failed: {embed_resp.text}"
    )
    embed_data = embed_resp.json()

    # 4. Upsert
    collection = (
        structure_data.get("routing", {}).get("collection")
        or f"{use_case}_default"
    )
    upsert_items = []
    for chunk, emb in zip(chunks, embed_data["embeddings"]):
        upsert_items.append({
            "chunk_id": chunk.get("id", emb.get("chunk_id", "")),
            "vector": emb["vector"],
            "metadata": {
                "text": chunk["text"][:500],
                **chunk.get("metadata", {}),
            },
        })

    upsert_resp = client.post(
        f"{_url('vectordb')}/v1/upsert",
        json={"collection": collection, "embeddings": upsert_items},
    )
    assert upsert_resp.status_code == 200, (
        f"Upsert failed: {upsert_resp.text}"
    )

    return {
        "collection": collection,
        "chunks": len(chunks),
        "upserted": upsert_resp.json().get("upserted", 0),
    }


def _query_agent(
    client: httpx.Client,
    query: str,
    use_case: str,
    role: str = "default",
    collection: str = "",
) -> dict:
    """Send a query through the agent endpoint."""
    session_id = str(uuid.uuid4())
    config = {"max_steps": 5}
    if collection:
        config["collection"] = collection

    resp = client.post(
        f"{_url('evaluation')}/v1/agent/query",
        json={
            "query": query,
            "use_case": use_case,
            "session_id": session_id,
            "role": role,
            "config": config,
        },
    )
    assert resp.status_code == 200, f"Agent query failed: {resp.text}"
    return resp.json()


# ── Health checks ─────────────────────────────────────────────────


SERVICES = [
    "cleaning",
    "data_structure",
    "embedding",
    "vectordb",
    "evaluation",
    "frontend",
]


@pytest.mark.parametrize("service", SERVICES)
def test_health(service):
    """All services respond to /health with 200."""
    with _client() as client:
        resp = client.get(f"{_url(service)}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ── Scenario 1: Firma Neumann ────────────────────────────────────


class TestNeumann:
    """PDF upload → query about max operating pressure."""

    def test_ingest(self):
        fixture = FIXTURES / "neumann_hydraulik.pdf"
        if not fixture.exists():
            pytest.skip("Fixture neumann_hydraulik.pdf not found")
        with _client() as client:
            result = _ingest(client, fixture, "neumann")
        assert result["upserted"] > 0

    def test_query(self):
        with _client() as client:
            result = _query_agent(
                client,
                query="Was ist der maximale Betriebsdruck?",
                use_case="neumann",
                collection="neumann_machines",
            )
        assert result["answer"], "Answer is empty"
        assert result["sufficient"] is True
        assert len(result["agent_steps"]) >= 1
        has_search = any(
            s["action"] == "SEARCH" for s in result["agent_steps"]
        )
        assert has_search, "Agent did not execute a SEARCH step"


# ── Scenario 2: GW St. Poelten ───────────────────────────────────


class TestGWStPoelten:
    """CNC Ruestmappe upload → query about missing tool."""

    def test_ingest(self):
        fixture = FIXTURES / "gw_cnc_ruest.pdf"
        if not fixture.exists():
            pytest.skip("Fixture gw_cnc_ruest.pdf not found")
        with _client() as client:
            result = _ingest(client, fixture, "gw_stpoelten")
        assert result["upserted"] > 0

    def test_query(self):
        with _client() as client:
            result = _query_agent(
                client,
                query=(
                    "Fraeskopf fehlt, Ruestungs-ID TEST-001"
                    " – Alternative?"
                ),
                use_case="gw_stpoelten",
                collection="gw_cnc_steps",
            )
        assert result["answer"], "Answer is empty"
        assert len(result["agent_steps"]) >= 1


# ── Scenario 3: Wiener Linien ────────────────────────────────────


class TestWienerLinien:
    """Vorschrift-Dokument upload → trainee query about paragraph."""

    def test_ingest(self):
        fixture = FIXTURES / "wl_vorschrift.pdf"
        if not fixture.exists():
            pytest.skip("Fixture wl_vorschrift.pdf not found")
        with _client() as client:
            result = _ingest(client, fixture, "wiener_linien")
        assert result["upserted"] > 0

    def test_query_trainee(self):
        with _client() as client:
            result = _query_agent(
                client,
                query="Was bedeutet Paragraph 34?",
                use_case="wiener_linien",
                role="trainee",
                collection="wl_fahrzeug",
            )
        assert result["answer"], "Answer is empty"
        assert len(result["agent_steps"]) >= 1


# ── Feedback ──────────────────────────────────────────────────────


def test_feedback_positive():
    """Feedback endpoint accepts positive rating."""
    with _client() as client:
        resp = client.post(
            f"{_url('evaluation')}/v1/log",
            json={
                "query_id": str(uuid.uuid4()),
                "feedback": "positive",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_feedback_negative():
    """Feedback endpoint accepts negative rating."""
    with _client() as client:
        resp = client.post(
            f"{_url('evaluation')}/v1/log",
            json={
                "query_id": str(uuid.uuid4()),
                "feedback": "negative",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["feedback"] == "negative"

"""SEARCH durchsucht ohne ausdrückliche Wahl alle Collections des Use Case.

Vorher wurde nur die Default-Collection gelesen: bei GW St. Pölten
``gw_cnc_steps`` – Einstellblätter und Fotos in ``gw_ruest_data`` blieben für
die Suche unsichtbar, ebenso Tabellen in ``<id>_default``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from core.actions import action_search
from main import app


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _Index:
    COLLECTIONS = ["gw_cnc_steps", "gw_ruest_data", "gw_stpoelten_default",
                   "neumann_machines", "wl_default"]

    def __init__(self):
        self.searched: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _Resp({"collections": [{"name": c} for c in self.COLLECTIONS]})

    async def post(self, url, json=None, **kw):
        if url.endswith("/v1/embed"):
            return _Resp({"vector": [0.1], "model": "m"})
        self.searched.append(json["collection"])
        return _Resp({"results": [{"score": 0.5, "metadata": {
            "text": f"Treffer aus {json['collection']}", "file_name": "x.pdf"}}]})


async def _search(args):
    index = _Index()
    with patch("core.actions.httpx.AsyncClient", lambda *a, **k: index), \
         patch("core.actions.hybrid_fuse", lambda q, results: results), \
         patch("core.actions.rerank_chunks",
               lambda q, chunks, top_n=15: [{**c, "rerank_score": 1.0} for c in chunks[:top_n]]):
        res = await action_search(args, use_case="gw_stpoelten")
    return res, index


@pytest.mark.anyio
async def test_without_collection_every_collection_of_the_use_case_is_searched():
    res, index = await _search({"query": "Wie wird gespannt?"})
    assert sorted(index.searched) == ["gw_cnc_steps", "gw_ruest_data", "gw_stpoelten_default"]
    assert sorted(res["searched_collections"]) == sorted(index.searched)


@pytest.mark.anyio
async def test_other_use_cases_stay_isolated():
    _, index = await _search({"query": "Wie wird gespannt?"})
    assert "neumann_machines" not in index.searched
    assert "wl_default" not in index.searched


@pytest.mark.anyio
async def test_explicit_collection_still_narrows_the_search():
    _, index = await _search({"query": "x", "collection": "gw_ruest_data"})
    assert index.searched == ["gw_ruest_data"]


@pytest.mark.anyio
async def test_chat_without_collection_does_not_pin_the_default():
    """The chat sends ``config: {}`` – that must reach the agent as "no choice"."""
    run = AsyncMock(return_value={"answer": "ok", "agent_steps": [], "citations": []})
    pool = MagicMock()
    pool.execute = AsyncMock()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("router.v1.run_manager", new=run), \
             patch("router.v1.get_pool", new=AsyncMock(return_value=pool)), \
             patch("router.v1.get_system_prompt", new=AsyncMock(return_value="p")):
            resp = await client.post("/v1/agent/query", json={
                "query": "Wie wird gespannt?", "use_case": "gw_stpoelten",
                "session_id": "00000000-0000-0000-0000-000000000001",
            })
    assert resp.status_code == 200
    assert run.await_args.kwargs["collection"] == ""


@pytest.mark.anyio
async def test_collections_endpoint_reports_prefixes():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("router.v1.httpx.AsyncClient", lambda *a, **k: _Index()):
            resp = await client.get("/v1/use-case/gw_stpoelten/collections")
    body = resp.json()
    assert body["prefixes"] == ["gw_"]
    assert {c["name"] for c in body["collections"]} == {
        "gw_cnc_steps", "gw_ruest_data", "gw_stpoelten_default"}

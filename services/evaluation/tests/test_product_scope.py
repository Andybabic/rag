"""Bauteile nicht vermischen: Suche in GW St. Pölten.

„Einstellblatt 1.pdf" gibt es in fast jedem Produktordner. Fragt jemand nach
einem Bauteil, dürfen Rüstangaben eines Nachbarteils nicht als Antwort
auftauchen – und wo Treffer mehrerer Bauteile zusammenkommen, muss der Agent
sie auseinanderhalten können.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.actions import _mixed_products_note, _source_label, action_search, product_scope


def _meta(pid, machine=None, file_name="Einstellblatt 1.pdf", page=1, text="Spannen"):
    return {"file_name": file_name, "page": page, "text": text,
            "extra": {"product_id": pid, "machine": machine}}


# ── product_scope ─────────────────────────────────────────────


def test_scope_to_the_one_product_a_question_names():
    assert product_scope("Wie wird 11221.7199 gespannt?", "gw_stpoelten") == "11221.7199"


def test_several_products_are_a_comparison_and_stay_open():
    assert product_scope("Unterschied zwischen 11221.7199 und 11221.7238?", "gw_stpoelten") is None


def test_no_product_no_scope():
    assert product_scope("Welches Werkzeug für Taschenfräsen?", "gw_stpoelten") is None


def test_scope_only_for_gw():
    assert product_scope("11221.7199", "neumann") is None


def test_file_names_and_drawing_numbers_are_not_products():
    assert product_scope("Programm 112217199_1BEAR", "gw_stpoelten") is None
    assert product_scope("Zeichnung 6AAE00000067864-B", "gw_stpoelten") is None


# ── Labels und Hinweise ───────────────────────────────────────


def test_label_names_product_and_machine():
    assert _source_label(_meta("11221.7199", "Chiron 243")) == (
        "Einstellblatt 1.pdf, S. 1 · Bauteil 11221.7199, Chiron 243"
    )


def test_label_does_not_repeat_what_the_name_already_says():
    meta = _meta("11221.7199", "Chiron 243",
                 file_name="11221.7199 (Chiron 243) – Einstellblatt 1.pdf")
    assert _source_label(meta) == "11221.7199 (Chiron 243) – Einstellblatt 1.pdf, S. 1"


def test_label_without_product_is_unchanged():
    assert _source_label({"file_name": "Handbuch.pdf", "page": 3}) == "Handbuch.pdf, S. 3"


def test_note_only_when_products_are_mixed():
    assert _mixed_products_note([_meta("11221.7199"), _meta("11221.7199")]) == ""
    note = _mixed_products_note([_meta("11221.7199"), _meta("11221.7238")])
    assert "2 verschiedenen Bauteilen" in note and "11221.7238" in note


# ── action_search ─────────────────────────────────────────────


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _Index:
    """Embedding + VectorDB in einem: filtert wie Qdrant nach extra.product_id."""

    def __init__(self, hits):
        self.hits = hits
        self.filters_seen: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _Resp({"collections": [{"name": "gw_ruest_data"}]})

    async def post(self, url, json=None, **kw):
        if url.endswith("/v1/embed"):
            return _Resp({"vector": [0.1, 0.2], "model": "m"})
        flt = json.get("filters") or {}
        self.filters_seen.append(flt)
        pid = flt.get("extra.product_id")
        return _Resp({"results": [
            {"score": 0.9, "metadata": dict(h)} for h in self.hits
            if pid is None or h["extra"]["product_id"] == pid
        ]})


async def _search(query, hits):
    index = _Index(hits)
    with patch("core.actions.httpx.AsyncClient", lambda *a, **k: index), \
         patch("core.actions.hybrid_fuse", lambda q, results: results), \
         patch("core.actions.rerank_chunks",
               lambda q, chunks, top_n=15: [{**c, "rerank_score": 1.0} for c in chunks[:top_n]]):
        # The agent always names a collection; without one SEARCH searches nothing.
        res = await action_search({"query": query, "collection": "gw_ruest_data"},
                                  use_case="gw_stpoelten")
    return res, index


_HITS = [
    _meta("11221.7199", "Chiron 243", text="Anschlag links, Spannkraft 50 Nm"),
    _meta("11221.7238", "Chiron 243", text="Anschlag rechts, Spannkraft 30 Nm"),
]


@pytest.mark.anyio
async def test_question_about_one_product_only_sees_that_product():
    res, index = await _search("Wie wird 11221.7199 gespannt?", _HITS)
    assert all(f.get("extra.product_id") == "11221.7199" for f in index.filters_seen)
    assert "50 Nm" in res["observation"]
    assert "30 Nm" not in res["observation"]
    assert "Hinweis" not in res["observation"]


@pytest.mark.anyio
async def test_unknown_product_falls_back_with_a_warning():
    res, _ = await _search("Wie wird 11221.9999 gespannt?", _HITS)
    obs = res["observation"]
    assert obs.startswith("Hinweis: Zu Bauteil 11221.9999 gibt es keine Daten")
    assert "gelten nicht" in obs


@pytest.mark.anyio
async def test_general_question_marks_mixed_products():
    res, index = await _search("Wie wird gespannt?", _HITS)
    assert all("extra.product_id" not in f for f in index.filters_seen)
    obs = res["observation"]
    assert "2 verschiedenen Bauteilen" in obs
    assert "Bauteil 11221.7199, Chiron 243" in obs
    assert "Bauteil 11221.7238, Chiron 243" in obs

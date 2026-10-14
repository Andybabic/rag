"""Retrieval around a planner's sub-query: anchor search, display, context."""

from __future__ import annotations

import core.actions as actions
import pytest
from core.actions import (
    _anchor_extras,
    _display_text,
    _search_queries,
    _strip_overlap,
    _with_context,
)


def _meta(text: str, crumb: str = "", file_name: str = "a.md") -> dict:
    return {"text": text, "file_name": file_name, "extra": {"breadcrumb": crumb}}


def test_anchor_added_only_when_it_differs(monkeypatch):
    monkeypatch.setattr(actions.settings, "SEARCH_ANCHOR", True)
    assert _search_queries("Regisseur?", "Ganze Frage?") == ["Regisseur?", "Ganze Frage?"]
    assert _search_queries("Frage?", " frage? ") == ["Frage?"]
    assert _search_queries("Frage?", "") == ["Frage?"]
    monkeypatch.setattr(actions.settings, "SEARCH_ANCHOR", False)
    assert _search_queries("Regisseur?", "Ganze Frage?") == ["Regisseur?"]


def test_display_drops_index_prefix_and_duplicate_heading():
    text = "[a.md | site.com – 2024-07-08]\n\n# site.com – 2024-07-08\n\nQuelle: x\n\nBody."
    assert _display_text(_meta(text, "site.com – 2024-07-08")) == "Quelle: x\n\nBody."


def test_display_keeps_a_heading_that_is_content():
    text = "[a.md | Kapitel 5 > Bremsen]\n\n# Bremsprüfung\n\nText"
    assert _display_text(_meta(text, "Kapitel 5 > Bremsen")) == "# Bremsprüfung\n\nText"


def test_overlap_between_neighbouring_chunks_is_removed():
    assert _strip_overlap("… killed at least six people in Beirut.",
                          "six people in Beirut. Officials said") == " Officials said"
    assert _strip_overlap("abc", "xyz") == "xyz"


def test_context_marks_what_is_neighbour(monkeypatch):
    monkeypatch.setattr(actions.settings, "CONTEXT_NEIGHBOR_CHARS", 10)
    out = _with_context("HIT", "0123456789-before", "after-0123456789")
    assert out == "(davor: …789-before)\nHIT\n(danach: after-0123…)"
    monkeypatch.setattr(actions.settings, "CONTEXT_NEIGHBOR_CHARS", 0)
    assert _with_context("HIT", "b", "a") == "HIT"


def _hit(cid: str, score: float, text: str = "") -> dict:
    return {"chunk_id": cid, "score": score, "_query_index": 1,
            "metadata": {"text": text or cid, "chunk_id": cid}}


def _rerank_by_word(word: str):
    def fake(query, chunks, top_n):
        return [{**c, "relevance_label": "relevant" if word in c["metadata"]["text"]
                 else "not_relevant"} for c in chunks][:top_n]
    return fake


@pytest.mark.anyio
async def test_anchor_slot_takes_a_relevant_hit_the_sub_query_missed(monkeypatch):
    """The Hamlet case: only the original question finds it."""
    monkeypatch.setattr(actions, "rerank_chunks", _rerank_by_word("Hamlet"))
    monkeypatch.setattr(actions.settings, "ANCHOR_SLOTS", 1)
    hits = [_hit("director", 0.9, "Lanthimos directed"),
            _hit("hamlet", 0.7, "Joe Alwyn stars in Hamlet")]
    extras = await _anchor_extras("Joe Alwyn Film?", hits, taken={"director"})
    assert [e["chunk_id"] for e in extras] == ["hamlet"]
    assert extras[0]["_via_anchor"] is True


@pytest.mark.anyio
async def test_anchor_slot_stays_empty_without_relevant_hit(monkeypatch):
    """No relevant original-question hit: the sub-query keeps all slots."""
    monkeypatch.setattr(actions, "rerank_chunks", _rerank_by_word("Hamlet"))
    monkeypatch.setattr(actions.settings, "ANCHOR_SLOTS", 1)
    assert await _anchor_extras("Frage", [_hit("a", 0.9)], taken=set()) == []
    assert await _anchor_extras("", [_hit("hamlet", 0.9, "Hamlet")], taken=set()) == []
    # already among the sub-query's hits → not counted twice
    assert await _anchor_extras("Frage", [_hit("h", 0.9, "Hamlet")], taken={"h"}) == []

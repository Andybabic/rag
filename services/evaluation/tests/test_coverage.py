"""Coverage: what each sub-query delivered, checked against the pool."""

from __future__ import annotations

import core.manager as manager
import pytest
from core.manager import _coverage_line, _find_gaps, assess_coverage, facet_status


def _sub(answer: str, *, error: str | None = None, queries=()) -> dict:
    return {
        "sub_query": "Wie viele Verletzte?",
        "answer": answer,
        "error": error,
        "chunks": [],
        "agent_steps": [{"action": "SEARCH", "args": {"query": q}} for q in queries],
    }


def test_status_from_fragment():
    assert facet_status(_sub("Es gab 172 Verletzte [2].")) == "found"
    assert facet_status(_sub("Dazu enthalten die Dokumente keine Information.")) == "not_found"
    assert facet_status(_sub(
        "In Tulkarm starben 18 Menschen [1]. Zu Beirut enthalten die Dokumente keine Information."
    )) == "partial"
    assert facet_status(_sub("")) == "not_found"
    assert facet_status(_sub("", error="timeout")) == "error"


def test_coverage_line_tells_open_from_missed():
    missed = {"status": "not_found", "searched": ["Verletzte Libanon"], "pool_candidates": [3, 7]}
    assert "[3], [7]" in _coverage_line(missed)
    assert "Gesucht: Verletzte Libanon" in _coverage_line(missed)
    open_ = {"status": "not_found", "searched": [], "pool_candidates": []}
    assert "offen" in _coverage_line(open_)
    unknown = {"status": "not_found", "searched": [], "pool_candidates": None}
    assert "Reranker" not in _coverage_line(unknown)


@pytest.mark.anyio
async def test_open_facet_is_checked_against_the_pool(monkeypatch):
    """The de:804 case: the 172 sat in the pool, the specialist said 'nothing'."""
    def fake_rerank(query, chunks, top_n):
        return [{"chunk_id": c["chunk_id"],
                 "relevance_label": "relevant" if "172" in c["metadata"]["text"] else "not_relevant"}
                for c in chunks]
    monkeypatch.setattr(manager, "rerank_chunks", fake_rerank)
    monkeypatch.setattr(manager.settings, "COVERAGE_CHECK", True)
    pool = [{"text": "18 Tote in Tulkarm"}, {"text": "95 killed and 172 wounded"}]
    cov = await assess_coverage(
        "Was forderte mehr Verletzte?",
        [_sub("Dazu enthalten die Dokumente keine Information.", queries=["Libanon 1982"]),
         _sub("Es gab 2.750 Verletzte [1].")],
        pool,
    )
    assert cov[0]["status"] == "not_found"
    assert cov[0]["pool_candidates"] == [2]
    assert cov[0]["searched"] == ["Libanon 1982"]
    assert cov[1]["pool_candidates"] is None  # found – no check needed


@pytest.mark.anyio
async def test_without_reranker_judgement_nothing_is_claimed(monkeypatch):
    monkeypatch.setattr(manager, "rerank_chunks", lambda q, c, top_n: [
        {"chunk_id": x["chunk_id"], "relevance_label": "unranked"} for x in c])
    monkeypatch.setattr(manager.settings, "COVERAGE_CHECK", True)
    cov = await assess_coverage("q", [_sub("keine Information.")], [{"text": "x"}])
    assert cov[0]["pool_candidates"] is None


def test_gap_only_when_open_and_nothing_in_pool():
    open_nothing = [{"sub_query": "A", "status": "not_found", "searched": ["a"], "pool_candidates": []}]
    open_missed = [{"sub_query": "A", "status": "not_found", "searched": [], "pool_candidates": [2]}]
    common = {"use_case": "xrag_bench", "subagents": [], "global_chunks": [{"text": "x"}]}
    assert len(_find_gaps("Frage", coverage=open_nothing, **common)) == 1
    assert _find_gaps("Frage", coverage=open_missed, **common) == []


def test_internal_vocabulary_is_removed_from_answers():
    from core.manager import _clean_synthesized
    assert _clean_synthesized(
        "Offen, da der GLOBAL_CHUNK_POOL hierzu keine Angaben enthält [3]."
    ) == "Offen, da der Quellenbestand hierzu keine Angaben enthält [3]."
    assert _clean_synthesized("Laut den Chunks [1] steht im Chunk [2] mehr.") == (
        "Laut den Textabschnitten [1] steht im Textabschnitt [2] mehr."
    )

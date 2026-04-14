"""Tests for the citation mapping module."""

from __future__ import annotations

from core.citations import map_citations


def _chunks(n: int) -> list[dict]:
    return [
        {
            "chunk_id": f"id-{i}",
            "text": f"Source text for chunk {i} with enough detail. " * 5,
            "score": 0.9 - i * 0.1,
            "metadata": {"file_name": f"doc_{i}.pdf", "page": i + 1},
        }
        for i in range(n)
    ]


def test_citations_maps_single_ref():
    answer = "Der Druck beträgt 10 bar [1]."
    result = map_citations(answer, _chunks(3))
    assert len(result) == 1
    assert result[0]["ref"] == "[1]"
    assert result[0]["file_name"] == "doc_0.pdf"
    assert result[0]["page"] == 1


def test_citations_maps_multiple_refs():
    answer = "Laut [1] und [2] ist der Wert korrekt [3]."
    result = map_citations(answer, _chunks(3))
    assert len(result) == 3
    refs = [c["ref"] for c in result]
    assert refs == ["[1]", "[2]", "[3]"]


def test_citations_deduplicates_refs():
    answer = "Siehe [1]. Nochmal [1]."
    result = map_citations(answer, _chunks(3))
    assert len(result) == 1


def test_citations_ignores_out_of_range():
    answer = "Referenz [5] existiert nicht."
    result = map_citations(answer, _chunks(3))
    assert len(result) == 0


def test_citations_ignores_zero_ref():
    answer = "Referenz [0] ist ungültig."
    result = map_citations(answer, _chunks(3))
    assert len(result) == 0


def test_citations_excerpt_max_200_chars():
    chunks = [
        {
            "chunk_id": "id-0",
            "text": "A" * 300,
            "score": 0.9,
            "metadata": {"file_name": "doc.pdf", "page": 1},
        }
    ]
    result = map_citations("Siehe [1].", chunks)
    assert len(result[0]["excerpt"]) == 200


def test_citations_preserves_score():
    result = map_citations("Siehe [1].", _chunks(1))
    assert result[0]["score"] == 0.9


def test_citations_no_refs_in_answer():
    result = map_citations("Keine Referenzen hier.", _chunks(3))
    assert result == []


def test_citations_order_matches_appearance():
    answer = "Erst [2], dann [1]."
    result = map_citations(answer, _chunks(3))
    assert result[0]["ref"] == "[2]"
    assert result[1]["ref"] == "[1]"

"""Tests for the chunking logic."""

from __future__ import annotations

import uuid

from core.chunker import chunk_markdown


def _chunk(markdown: str, **kwargs):
    defaults = {
        "file_name": "test.pdf",
        "doc_type": "pdf",
        "use_case": "neumann",
        "collection": "neumann_machines",
    }
    return chunk_markdown(markdown, **(defaults | kwargs))


def test_basic_chunking():
    md = "This is a simple paragraph of text that should become a single chunk."
    chunks = _chunk(md)
    assert len(chunks) >= 1
    assert chunks[0].text.strip()


def test_each_chunk_has_unique_uuid():
    md = " ".join([f"Sentence number {i}." for i in range(50)])
    chunks = _chunk(md, chunk_size=100, chunk_overlap=10)
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids))
    for cid in ids:
        uuid.UUID(cid)  # validates UUID format


def test_chunk_metadata_fields():
    md = "Some content."
    chunks = _chunk(md, extra={"machine_id": "M-4711"})
    c = chunks[0]
    assert c.metadata.file_name == "test.pdf"
    assert c.metadata.doc_type == "pdf"
    assert c.metadata.use_case == "neumann"
    assert c.metadata.collection == "neumann_machines"
    assert c.metadata.extra == {"machine_id": "M-4711"}


def test_page_anchors_single_page():
    md = "<!-- page:1 -->Content on page one."
    chunks = _chunk(md)
    assert chunks[0].metadata.page == 1
    assert "<!-- page:" not in chunks[0].text


def test_page_anchors_multi_page():
    pages = []
    for i in range(1, 6):
        pages.append(f"<!-- page:{i} -->\nThis is content on page {i}. " * 5)
    md = "\n\n".join(pages)
    chunks = _chunk(md, chunk_size=100, chunk_overlap=10)

    # Should have chunks from multiple pages
    page_numbers = {c.metadata.page for c in chunks}
    assert len(page_numbers) > 1
    # All page numbers should be in 1-5 range
    for p in page_numbers:
        assert p is not None
        assert 1 <= p <= 5


def test_five_pages_correct_page_numbers():
    """Markdown with 5 pages → chunks have correct page numbers."""
    parts = []
    for i in range(1, 6):
        # Each page has enough text to likely form its own chunk(s)
        text = f"Page {i} content. " * 30
        parts.append(f"<!-- page:{i} -->\n{text}")
    md = "\n\n".join(parts)
    chunks = _chunk(md, chunk_size=200, chunk_overlap=20)

    assert len(chunks) >= 5  # at least one chunk per page
    # First chunk should be page 1
    assert chunks[0].metadata.page == 1
    # Last chunk should be page 5
    assert chunks[-1].metadata.page == 5


def test_no_page_anchors():
    md = "Content without any page markers."
    chunks = _chunk(md)
    assert chunks[0].metadata.page is None


def test_chunk_size_respected():
    md = " ".join([f"Word{i}" for i in range(200)])
    chunks = _chunk(md, chunk_size=100, chunk_overlap=10)
    assert len(chunks) > 1


def test_total_pages_in_metadata():
    md = "Content."
    chunks = _chunk(md, total_pages=45)
    assert chunks[0].metadata.total_pages == 45


def test_empty_markdown():
    chunks = _chunk("")
    assert chunks == []


def test_chunk_id_equals_metadata_chunk_id():
    md = "Some text."
    chunks = _chunk(md)
    for c in chunks:
        assert c.id == c.metadata.chunk_id

"""Tests for shared Pydantic models."""

import uuid

from shared.models import APIError, Chunk, ChunkMetadata, EmbeddedChunk, SearchResult


def _make_metadata(**overrides):
    defaults = {
        "file_name": "manual.pdf",
        "doc_type": "pdf",
        "use_case": "neumann",
        "collection": "neumann_docs",
    }
    return ChunkMetadata(**(defaults | overrides))


def test_chunk_metadata_auto_generates_uuid():
    meta = _make_metadata()
    uuid.UUID(meta.chunk_id)  # raises if not valid UUID


def test_chunk_metadata_extra_defaults_to_empty_dict():
    meta = _make_metadata()
    assert meta.extra == {}


def test_chunk_metadata_optional_fields():
    meta = _make_metadata(page=3, total_pages=10)
    assert meta.page == 3
    assert meta.total_pages == 10


def test_chunk_auto_generates_id():
    meta = _make_metadata()
    chunk = Chunk(text="some text", metadata=meta)
    uuid.UUID(chunk.id)


def test_embedded_chunk():
    meta = _make_metadata()
    chunk = Chunk(text="text", metadata=meta)
    ec = EmbeddedChunk(chunk=chunk, vector=[0.1, 0.2], model="nomic", dimension=2)
    assert ec.dimension == 2
    assert ec.vector == [0.1, 0.2]


def test_search_result():
    meta = _make_metadata()
    chunk = Chunk(text="text", metadata=meta)
    sr = SearchResult(chunk=chunk, score=0.95)
    assert sr.score == 0.95


def test_api_error():
    err = APIError(
        error="ValueError",
        detail="bad input",
        request_id="abc-123",
        service="cleaning",
    )
    assert err.service == "cleaning"

"""Tests for PDFHandler ↔ mineru-api /file_parse integration (mocked HTTP).

Reflects the current contract:
  * single POST to /file_parse (mineru-api paginates itself)
  * response: {"results": {"<stem>": {"md_content","content_list","images"}}}
  * NO PyMuPDF fallback while USE_MINERU=true – MinerU failure is fatal
  * degenerate (near-empty) extraction is rejected, never silently ingested
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import fitz
import httpx
import pytest
from core.handlers import PDFHandler
from models import DegenerateExtractionError, MaxRetriesExceeded


def _make_pdf_bytes(page_texts: list[str]) -> bytes:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _image_only_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 1)
    pix.clear_with(255)
    page.insert_image(fitz.Rect(50, 50, 150, 150), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def _fp_response(content_list: list[dict], md: str = "", images: dict | None = None) -> dict:
    """Build a mineru-api /file_parse JSON response."""
    return {
        "backend": "pipeline",
        "version": "test",
        "results": {
            "doc": {
                "md_content": md,
                "content_list": content_list,
                "images": images or {},
            }
        },
    }


@pytest.fixture()
def _enable_mineru(monkeypatch):
    monkeypatch.setattr("config.settings.USE_MINERU", True)
    monkeypatch.setattr("config.settings.MAX_RETRIES", 3)
    monkeypatch.setattr("config.settings.RETRY_BACKOFF_BASE", 0)  # no wait in tests


# ── /file_parse response → ParsedDocument ────────────────────


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_pages_built_from_content_list():
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["p1", "p2"])
    content = [
        {"type": "text", "text": "First page.", "page_idx": 0},
        {"type": "text", "text": "Second page.", "page_idx": 1},
    ]
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response(content)
        result = await handler.parse(pdf, "doc.pdf")

    assert result.metadata["parser"] == "mineru"
    assert result.metadata["backend"] == "pipeline"
    assert result.metadata["total_pages"] == 2
    assert [p["page"] for p in result.pages] == [1, 2]
    assert "First page." in result.pages[0]["text"]
    assert "<!-- page:1 -->" in result.text
    assert "<!-- page:2 -->" in result.text


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_html_tables_in_md_content_become_pipe():
    """md_content from MinerU embeds <table> blocks as raw HTML.
    Those must be converted to pipe so chunks aren't dropped by the
    alpha-ratio boilerplate filter (observed: §11 lost entirely)."""
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    md = (
        "# § 11 Ersatzsignal\n\n"
        "<table>"
        "<tr><td rowspan=1 colspan=1>Ersatzsignal</td>"
        "<td>Ein weißes, blinkendes Licht am Armaturenpult</td></tr>"
        "<tr><td>Fa 20</td><td>Vorbeifahrt gestattet</td></tr>"
        "</table>\n\n"
        "Weiterer Prosa-Absatz."
    )
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response([], md=md)
        result = await handler.parse(pdf, "sig.pdf")

    assert "<td" not in result.text and "rowspan" not in result.text
    assert "| Ersatzsignal |" in result.text
    assert "Armaturenpult" in result.text
    assert "Weiterer Prosa-Absatz" in result.text


def test_html_table_converted_to_pipe_text():
    """HTML <table> chunks were dominating embeddings with markup noise
    (<td rowspan=1 colspan=1>...) – top scores collapsed to ~0.06 on
    verbatim queries. Convert to pipe-markdown so embeddings capture
    the actual cell content."""
    html = (
        "<table>"
        "<tr><td rowspan=1 colspan=1>Ersatzsignal</td>"
        "<td>Ein weißes, blinkendes Licht am Armaturenpult</td></tr>"
        "<tr><td>Fa 20</td><td>Vorbeifahrt gestattet</td></tr>"
        "</table>"
    )
    out = PDFHandler._html_table_to_pipe(html)
    assert "<td" not in out and "rowspan" not in out
    assert out.count("|") >= 4  # → exempt from boilerplate filter
    assert "Ersatzsignal" in out and "Armaturenpult" in out
    assert "Fa 20" in out


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_table_body_is_searchable():
    """The §11 regression: table content must survive verbatim."""
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    content = [
        {
            "type": "table",
            "table_caption": ["Signalbild"],
            "table_body": "<table><tr><td>Ersatzsignal</td>"
            "<td>Blinkt am Armaturenpult die weiße Drucktaste</td></tr></table>",
            "page_idx": 0,
        }
    ]
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response(content)
        result = await handler.parse(pdf, "sig.pdf")

    assert "Ersatzsignal" in result.text
    assert "Armaturenpult" in result.text


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_images_mapped_and_base64_stripped():
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    content = [
        {"type": "text", "text": "txt", "page_idx": 0},
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 2},
    ]
    images = {"a.jpg": "data:image/jpeg;base64,QUJD"}
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response(content, images=images)
        result = await handler.parse(pdf, "scan.pdf")

    assert len(result.images) == 1
    assert result.images[0]["base64"] == "QUJD"  # data-URI prefix stripped
    assert result.images[0]["page"] == 3  # page_idx 2 → page 3
    assert result.images[0]["caption"] == "a.jpg"


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_mineru_native_page_markers_are_kept():
    """MinerU emits "<!-- Page N -->" (capital P, space) directly in
    md_content. The injector must NOT add competing lowercase anchors
    – MinerU's markers are authoritative and the chunker recognises
    them now. Previously, half the document's pages had no usable
    page metadata because the injector's snippet-match missed them."""
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    md = (
        "<!-- Page 1 -->\n# Erste Seite\nInhalt eins.\n\n"
        "<!-- Page 14 -->\n# § 11 Ersatzsignal\nBlinkt am Armaturenpult …"
    )
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response([], md=md)
        result = await handler.parse(pdf, "sig.pdf")

    # MinerU markers passed through unchanged.
    assert "<!-- Page 1 -->" in result.text
    assert "<!-- Page 14 -->" in result.text
    # And no duplicate lowercase anchors injected on top.
    assert "<!-- page:1 -->" not in result.text
    assert "<!-- page:14 -->" not in result.text


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_md_content_is_primary_source():
    """Regression: ~80 % of document content was lost when text was rebuilt
    from content_list alone (9 chunks for a 23-page PDF). md_content is the
    comprehensive source; content_list only supplies page anchors."""
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    md = (
        "# § 11 Ersatzsignal\n\n"
        'Blinkt am Armaturenpult die weiße Drucktaste „Ersatzsignal".\n\n'
        "## (2) Signalbild\n\n"
        "| Ersatzsignal | Ein weißes, blinkendes Licht am Armaturenpult |\n"
        "Weiterer Prosa-Absatz mit Details zur Quittierung."
    )
    # content_list deliberately MUCH sparser than md_content – the old
    # implementation would have produced only the snippet from content_list.
    content = [
        {"type": "text", "text": "§ 11 Ersatzsignal", "page_idx": 0}
    ]
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response(content, md=md)
        result = await handler.parse(pdf, "sig.pdf")

    # Full md preserved → all keywords present in indexable text.
    assert "Armaturenpult" in result.text
    assert "Quittierung" in result.text
    assert "blinkendes Licht" in result.text
    # Page anchor injected at the snippet position so chunks keep page metadata.
    assert "<!-- page:1 -->" in result.text


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_content_list_arrives_as_json_string():
    """mineru-api returns content_list as the RAW file text (a JSON
    string), not a parsed list. It must be json-decoded, not iterated
    character-by-character ('str' object has no attribute 'get')."""
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    content = [{"type": "text", "text": "Aus JSON-String", "page_idx": 0}]
    resp = _fp_response([])
    resp["results"]["doc"]["content_list"] = json.dumps(content)  # string!
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = resp
        result = await handler.parse(pdf, "doc.pdf")

    assert "Aus JSON-String" in result.text
    assert result.pages[0]["page"] == 1


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_fallback_to_md_content_when_no_content_list():
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response([], md="Nur Markdown, keine Blöcke.")
        result = await handler.parse(pdf, "doc.pdf")

    assert result.text == "Nur Markdown, keine Blöcke."
    assert len(result.pages) == 1


# ── No PyMuPDF fallback while MinerU active ───────────────────


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_mineru_failure_raises_no_pymupdf_fallback():
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["would-be fallback text"])
    with patch.object(
        handler,
        "_do_mineru_request",
        new_callable=AsyncMock,
        side_effect=MaxRetriesExceeded("all retries failed"),
    ):
        with pytest.raises(DegenerateExtractionError):
            await handler.parse(pdf, "fail.pdf")


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_empty_mineru_result_raises_degenerate():
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as m:
        m.return_value = _fp_response([], md="")
        with pytest.raises(DegenerateExtractionError):
            await handler.parse(pdf, "empty.pdf")


# ── Retry wrapper ────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru")
async def test_retry_then_succeed():
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["x"])
    calls = 0

    async def flaky(file_bytes, filename):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.TimeoutException("timeout")
        return _fp_response([{"type": "text", "text": "OK after retry", "page_idx": 0}])

    with patch.object(handler, "_do_mineru_request", side_effect=flaky):
        result = await handler.parse(pdf, "retry.pdf")

    assert calls == 3
    assert result.metadata["parser"] == "mineru"
    assert "OK after retry" in result.text


# ── USE_MINERU=false → PyMuPDF, with degenerate guard ─────────


@pytest.mark.anyio
async def test_mineru_disabled_uses_pymupdf(monkeypatch):
    monkeypatch.setattr("config.settings.USE_MINERU", False)
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["Direct pymupdf text on the page"])
    result = await handler.parse(pdf, "direct.pdf")
    assert result.metadata["parser"] == "pymupdf"
    assert "Direct pymupdf" in result.pages[0]["text"]


@pytest.mark.anyio
async def test_digital_pdf_extracts_text(monkeypatch):
    monkeypatch.setattr("config.settings.USE_MINERU", False)
    handler = PDFHandler()
    pdf = _make_pdf_bytes(["Digital text page 1", "Digital text page 2"])
    result = await handler.parse(pdf, "digital.pdf")
    assert result.pages[0]["page"] == 1
    assert result.pages[1]["page"] == 2
    assert result.metadata["total_pages"] == 2


@pytest.mark.anyio
async def test_scanned_pdf_pymupdf_raises_degenerate(monkeypatch):
    """Scanned/image-only PDF without OCR must fail loudly, not ingest
    an empty index silently."""
    monkeypatch.setattr("config.settings.USE_MINERU", False)
    handler = PDFHandler()
    with pytest.raises(DegenerateExtractionError):
        await handler.parse(_image_only_pdf(), "scanned.pdf")

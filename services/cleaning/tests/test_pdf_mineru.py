"""Tests for PDFHandler MineU integration (mocked HTTP calls)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import fitz
import httpx
import pytest
from core.handlers import PDFHandler
from models import MaxRetriesExceeded


def _make_pdf_bytes(page_texts: list[str]) -> bytes:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


# ── Helpers ──────────────────────────────────────────────────


def _mineru_response_with_page_comments(pages: list[str]) -> dict:
    """Build a MineU-style JSON response with <!-- Page N --> markers."""
    parts = []
    for i, text in enumerate(pages, start=1):
        parts.append(f"<!-- Page {i} -->\n{text}")
    return {"markdown": "\n".join(parts), "images": []}


def _mineru_response_single_page(text: str, images: list | None = None) -> dict:
    return {"markdown": text, "images": images or []}


def _mineru_page_by_page_response(text: str, page_num: int) -> dict:
    return {"markdown": text, "images": []}


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def _enable_mineru(monkeypatch):
    monkeypatch.setattr("config.settings.USE_MINERU", True)
    monkeypatch.setattr("config.settings.MAX_RETRIES", 3)
    monkeypatch.setattr("config.settings.RETRY_BACKOFF_BASE", 0)  # no wait in tests


@pytest.fixture()
def _enable_mineru_page_by_page(monkeypatch, _enable_mineru):
    monkeypatch.setattr("config.settings.MINERU_PAGE_BY_PAGE", True)


@pytest.fixture()
def _enable_mineru_single(monkeypatch, _enable_mineru):
    monkeypatch.setattr("config.settings.MINERU_PAGE_BY_PAGE", False)


# ── MineU single-call mode ───────────────────────────────────


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_single")
async def test_mineru_single_with_page_comments():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["p1", "p2", "p3"])
    response = _mineru_response_with_page_comments(
        ["# Heading\nFirst page.", "Second page content.", "Third page."]
    )

    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as mock:
        mock.return_value = response
        result = await handler.parse(pdf_bytes, "doc.pdf")

    assert result.metadata["parser"] == "mineru"
    assert result.metadata["mode"] == "single"
    assert len(result.pages) == 3
    assert result.pages[0]["page"] == 1
    assert "First page" in result.pages[0]["text"]
    assert result.pages[1]["page"] == 2
    assert result.pages[2]["page"] == 3
    assert result.metadata["total_pages"] == 3


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_single")
async def test_mineru_single_no_page_comments():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["only one"])
    response = _mineru_response_single_page("Just markdown, no page markers.")

    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as mock:
        mock.return_value = response
        result = await handler.parse(pdf_bytes, "doc.pdf")

    assert result.metadata["parser"] == "mineru"
    assert result.metadata["total_pages"] == 1
    assert len(result.pages) == 1
    assert result.pages[0]["page"] == 1


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_single")
async def test_mineru_single_with_images():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["img page"])
    response = {
        "markdown": "<!-- Page 1 -->\nSome text with image",
        "images": [
            {"page": 1, "base64": "aW1hZ2VkYXRh", "caption": "Figure 1"},
        ],
    }

    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as mock:
        mock.return_value = response
        result = await handler.parse(pdf_bytes, "scan.pdf")

    assert len(result.images) == 1
    assert result.images[0]["page"] == 1
    assert result.images[0]["base64"] == "aW1hZ2VkYXRh"
    assert result.images[0]["caption"] == "Figure 1"


# ── MineU page-by-page mode ─────────────────────────────────


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_page_by_page")
async def test_mineru_page_by_page():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["p1", "p2"])

    call_count = 0

    async def fake_request(file_bytes, filename, start_page=None, end_page=None):
        nonlocal call_count
        call_count += 1
        return _mineru_page_by_page_response(f"Page {start_page + 1} content", start_page)

    with patch.object(handler, "_do_mineru_request", side_effect=fake_request):
        result = await handler.parse(pdf_bytes, "multi.pdf")

    assert call_count == 2  # one call per page
    assert len(result.pages) == 2
    assert result.pages[0]["page"] == 1
    assert result.pages[1]["page"] == 2
    assert result.metadata["parser"] == "mineru"
    assert result.metadata["mode"] == "page_by_page"
    assert result.metadata["total_pages"] == 2


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_page_by_page")
async def test_mineru_page_by_page_with_images():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["scanned page"])

    async def fake_request(file_bytes, filename, start_page=None, end_page=None):
        return {
            "markdown": "Scanned text",
            "images": [{"base64": "c2Nhbg==", "caption": ""}],
        }

    with patch.object(handler, "_do_mineru_request", side_effect=fake_request):
        result = await handler.parse(pdf_bytes, "scan.pdf")

    assert len(result.images) == 1
    assert result.images[0]["page"] == 1
    assert result.images[0]["base64"] == "c2Nhbg=="


# ── Fallback to PyMuPDF ─────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_single")
async def test_fallback_on_mineru_failure():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["Fallback content"])

    with patch.object(
        handler,
        "_do_mineru_request",
        new_callable=AsyncMock,
        side_effect=MaxRetriesExceeded("all retries failed"),
    ):
        result = await handler.parse(pdf_bytes, "fail.pdf")

    assert result.metadata["parser"] == "pymupdf"
    assert "Fallback content" in result.text


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_page_by_page")
async def test_fallback_on_mineru_timeout():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["Timeout content"])

    with patch.object(
        handler,
        "_do_mineru_request",
        new_callable=AsyncMock,
        side_effect=httpx.TimeoutException("read timeout"),
    ):
        result = await handler.parse(pdf_bytes, "timeout.pdf")

    # After MAX_RETRIES timeouts, MaxRetriesExceeded is raised inside
    # _call_mineru, which is caught by parse() and triggers fallback
    assert result.metadata["parser"] == "pymupdf"
    assert "Timeout content" in result.text


# ── Retry logic ──────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_single")
async def test_retry_then_succeed():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["retry content"])
    good_response = _mineru_response_with_page_comments(["Success after retry"])

    call_count = 0

    async def flaky_request(file_bytes, filename, start_page=None, end_page=None):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.TimeoutException("timeout")
        return good_response

    with patch.object(handler, "_do_mineru_request", side_effect=flaky_request):
        result = await handler.parse(pdf_bytes, "retry.pdf")

    assert call_count == 3
    assert result.metadata["parser"] == "mineru"
    assert "Success after retry" in result.text


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_single")
async def test_max_retries_exceeded():
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["will fail"])

    with patch.object(
        handler,
        "_do_mineru_request",
        new_callable=AsyncMock,
        side_effect=httpx.TimeoutException("timeout"),
    ):
        # parse() catches MaxRetriesExceeded and falls back
        result = await handler.parse(pdf_bytes, "maxretry.pdf")

    assert result.metadata["parser"] == "pymupdf"


# ── USE_MINERU=false → PyMuPDF directly ──────────────────────


@pytest.mark.anyio
async def test_mineru_disabled_uses_pymupdf(monkeypatch):
    monkeypatch.setattr("config.settings.USE_MINERU", False)
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["Direct pymupdf"])
    result = await handler.parse(pdf_bytes, "direct.pdf")
    assert result.metadata["parser"] == "pymupdf"


# ── Page comment splitting ───────────────────────────────────


def test_split_by_page_comments_basic():
    md = "<!-- Page 1 -->\nFirst\n<!-- Page 2 -->\nSecond"
    pages = PDFHandler._split_by_page_comments(md)
    assert pages is not None
    assert len(pages) == 2
    assert pages[0] == {"page": 1, "text": "First"}
    assert pages[1] == {"page": 2, "text": "Second"}


def test_split_by_page_comments_none_without_markers():
    assert PDFHandler._split_by_page_comments("no markers here") is None


def test_split_by_page_comments_whitespace_variants():
    md = "<!--  Page  3  -->\nContent"
    pages = PDFHandler._split_by_page_comments(md)
    assert pages is not None
    assert pages[0]["page"] == 3


# ── Digital vs scanned PDF (PyMuPDF) ─────────────────────────


@pytest.mark.anyio
async def test_digital_pdf_extracts_text():
    """Digital PDF with embedded text – PyMuPDF extracts it directly."""
    handler = PDFHandler()
    pdf_bytes = _make_pdf_bytes(["Digital text on page 1", "Digital text on page 2"])
    result = await handler.parse(pdf_bytes, "digital.pdf")
    assert "Digital text on page 1" in result.pages[0]["text"]
    assert result.pages[0]["page"] == 1
    assert result.pages[1]["page"] == 2
    assert result.metadata["total_pages"] == 2


@pytest.mark.anyio
async def test_scanned_pdf_empty_with_pymupdf():
    """Scanned PDF (image-only) – PyMuPDF returns no text.

    This verifies that scanned PDFs need MineU/OCR for text extraction.
    """
    doc = fitz.open()
    page = doc.new_page()
    # Insert a tiny pixmap (simulates a scanned image page with no text layer)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 1)
    pix.clear_with(255)
    page.insert_image(fitz.Rect(50, 50, 150, 150), pixmap=pix)
    pdf_bytes = doc.tobytes()
    doc.close()

    handler = PDFHandler()
    result = await handler.parse(pdf_bytes, "scanned.pdf")
    assert result.metadata["parser"] == "pymupdf"
    assert result.metadata["total_pages"] == 1
    # No text extracted from image-only page
    assert len(result.pages) == 0 or result.pages[0]["text"].strip() == ""


@pytest.mark.anyio
@pytest.mark.usefixtures("_enable_mineru_single")
async def test_scanned_pdf_with_mineru_ocr():
    """Scanned PDF + MineU → OCR produces text."""
    handler = PDFHandler()
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 1)
    pix.clear_with(255)
    page.insert_image(fitz.Rect(50, 50, 150, 150), pixmap=pix)
    pdf_bytes = doc.tobytes()
    doc.close()

    response = _mineru_response_with_page_comments(["OCR extracted: Wartungshandbuch Seite 1"])

    with patch.object(handler, "_do_mineru_request", new_callable=AsyncMock) as mock:
        mock.return_value = response
        result = await handler.parse(pdf_bytes, "scanned.pdf")

    assert result.metadata["parser"] == "mineru"
    assert "Wartungshandbuch" in result.text

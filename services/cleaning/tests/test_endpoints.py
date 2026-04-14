"""Tests for POST /v1/clean and POST /v1/clean/batch endpoints."""

from __future__ import annotations

import io
import json

import docx
import fitz
import httpx
import pytest
from main import app


@pytest.fixture()
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _make_pdf_bytes(page_texts: list[str]) -> bytes:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    doc = docx.Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── POST /v1/clean ───────────────────────────────────────────


@pytest.mark.anyio
async def test_clean_pdf(client):
    pdf = _make_pdf_bytes(["Wartungshandbuch Seite 1"])
    resp = await client.post(
        "/v1/clean",
        files={"file": ("manual.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "request_id" in body
    assert "Wartungshandbuch" in body["markdown"]
    assert body["metadata"]["file_name"] == "manual.pdf"
    assert body["metadata"]["doc_type"] == "pdf"
    assert body["metadata"]["total_pages"] >= 1


@pytest.mark.anyio
async def test_clean_txt(client):
    resp = await client.post(
        "/v1/clean",
        files={"file": ("notes.txt", b"Hello World", "text/plain")},
    )
    assert resp.status_code == 200
    assert resp.json()["markdown"] == "Hello World"


@pytest.mark.anyio
async def test_clean_docx(client):
    docx_bytes = _make_docx_bytes(["Paragraph one", "Paragraph two"])
    resp = await client.post(
        "/v1/clean",
        files={"file": ("report.docx", docx_bytes, "application/octet-stream")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "Paragraph one" in body["markdown"]
    assert body["metadata"]["doc_type"] == "docx"


@pytest.mark.anyio
async def test_clean_csv(client):
    resp = await client.post(
        "/v1/clean",
        files={"file": ("data.csv", b"A,B\n1,2", "text/csv")},
    )
    assert resp.status_code == 200
    assert "A | B" in resp.json()["markdown"]


@pytest.mark.anyio
async def test_clean_unsupported_format(client):
    resp = await client.post(
        "/v1/clean",
        files={"file": ("image.png", b"fake", "image/png")},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "unsupported_format"
    assert "request_id" in body


@pytest.mark.anyio
async def test_clean_has_request_id(client):
    resp = await client.post(
        "/v1/clean",
        files={"file": ("test.txt", b"hi", "text/plain")},
    )
    body = resp.json()
    assert body["request_id"]
    assert len(body["request_id"]) > 0


@pytest.mark.anyio
async def test_clean_pages_present(client):
    pdf = _make_pdf_bytes(["Page 1 text", "Page 2 text"])
    resp = await client.post(
        "/v1/clean",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
    )
    body = resp.json()
    assert len(body["pages"]) == 2
    assert body["pages"][0]["page"] == 1
    assert body["pages"][1]["page"] == 2


@pytest.mark.anyio
async def test_clean_extract_images_false(client):
    pdf = _make_pdf_bytes(["text"])
    resp = await client.post(
        "/v1/clean",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"config": json.dumps({"extract_images": False})},
    )
    body = resp.json()
    assert body["images"] == []


@pytest.mark.anyio
async def test_clean_pii_removal(client):
    text = b"Contact: test@example.com, Tel: +43 1 234567, IP: 10.0.0.1"
    resp = await client.post(
        "/v1/clean",
        files={"file": ("info.txt", text, "text/plain")},
        data={"config": json.dumps({"pii_removal": True})},
    )
    body = resp.json()
    assert "test@example.com" not in body["markdown"]
    assert "[EMAIL_REMOVED]" in body["markdown"]
    assert "[PHONE_REMOVED]" in body["markdown"]
    assert "[IP_REMOVED]" in body["markdown"]
    # Also check that pages have PII removed
    assert "test@example.com" not in body["pages"][0]["text"]


# ── POST /v1/clean/batch ─────────────────────────────────────


@pytest.mark.anyio
async def test_batch_multiple_files(client):
    files = [
        ("files", ("a.txt", b"File A", "text/plain")),
        ("files", ("b.txt", b"File B", "text/plain")),
        ("files", ("c.csv", b"X,Y\n1,2", "text/csv")),
    ]
    resp = await client.post("/v1/clean/batch", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total"] == 3
    assert body["success"] == 3
    assert body["failed"] == 0
    assert len(body["results"]) == 3
    assert "request_id" in body


@pytest.mark.anyio
async def test_batch_partial_failure(client):
    files = [
        ("files", ("good.txt", b"works fine", "text/plain")),
        ("files", ("bad.xyz", b"will fail", "application/octet-stream")),
    ]
    resp = await client.post("/v1/clean/batch", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["success"] == 1
    assert body["failed"] == 1
    assert len(body["results"]) == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["file_name"] == "bad.xyz"


@pytest.mark.anyio
async def test_batch_five_files(client):
    pdf = _make_pdf_bytes(["PDF content"])
    docx_data = _make_docx_bytes(["Docx content"])
    files = [
        ("files", ("doc1.pdf", pdf, "application/pdf")),
        ("files", ("doc2.docx", docx_data, "application/octet-stream")),
        ("files", ("doc3.txt", b"Text content", "text/plain")),
        ("files", ("doc4.csv", b"H\n1", "text/csv")),
        ("files", ("doc5.md", b"# Markdown", "text/markdown")),
    ]
    resp = await client.post("/v1/clean/batch", files=files)
    body = resp.json()
    assert body["total"] == 5
    assert body["success"] == 5
    assert body["failed"] == 0


@pytest.mark.anyio
async def test_batch_error_does_not_stop_others(client):
    """A failing file must not prevent other files from being processed."""
    files = [
        ("files", ("bad1.xyz", b"fail", "application/octet-stream")),
        ("files", ("good.txt", b"ok", "text/plain")),
        ("files", ("bad2.zzz", b"fail", "application/octet-stream")),
    ]
    resp = await client.post("/v1/clean/batch", files=files)
    body = resp.json()
    assert body["success"] == 1
    assert body["failed"] == 2


@pytest.mark.anyio
async def test_batch_error_logged(client, tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.LOG_DIR", str(tmp_path))
    files = [("files", ("fail.xyz", b"nope", "application/octet-stream"))]
    await client.post("/v1/clean/batch", files=files)
    log_file = tmp_path / "cleaning" / "batch_errors.jsonl"
    assert log_file.exists()
    content = log_file.read_text()
    assert "fail.xyz" in content


@pytest.mark.anyio
async def test_batch_pii_removal(client):
    files = [
        ("files", ("pii.txt", b"Mail: a@b.at", "text/plain")),
    ]
    resp = await client.post(
        "/v1/clean/batch",
        files=files,
        data={"config": json.dumps({"pii_removal": True})},
    )
    body = resp.json()
    assert "a@b.at" not in body["results"][0]["markdown"]
    assert "[EMAIL_REMOVED]" in body["results"][0]["markdown"]

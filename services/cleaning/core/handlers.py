"""File-type handlers.

Each handler inherits from BaseHandler and implements
async parse(file_bytes, filename) -> ParsedDocument.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import docx
import fitz  # PyMuPDF
import httpx
from config import settings
from models import MaxRetriesExceeded, ParsedDocument

logger = logging.getLogger(__name__)


class BaseHandler(ABC):
    supported_extensions: list[str] = []

    @abstractmethod
    async def parse(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        ...


class TextHandler(BaseHandler):
    supported_extensions = [".txt", ".md"]

    async def parse(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        text = file_bytes.decode("utf-8", errors="replace")
        return ParsedDocument(
            text=text,
            pages=[{"page": 1, "text": text}],
            images=[],
            metadata={"format": "text"},
        )


class CsvHandler(BaseHandler):
    supported_extensions = [".csv"]

    async def parse(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        decoded = file_bytes.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(decoded))
        rows = list(reader)
        if not rows:
            return ParsedDocument(text="", pages=[], images=[], metadata={"format": "csv"})

        header = rows[0]
        separator = " | ".join("---" for _ in header)
        lines = [" | ".join(header), separator]
        for row in rows[1:]:
            lines.append(" | ".join(row))
        text = "\n".join(lines)

        return ParsedDocument(
            text=text,
            pages=[{"page": 1, "text": " | ".join(header)}]
            + [{"page": i + 2, "text": " | ".join(row)} for i, row in enumerate(rows[1:])],
            images=[],
            metadata={"format": "csv", "row_count": len(rows) - 1, "columns": header},
        )


class DocxHandler(BaseHandler):
    supported_extensions = [".docx"]

    async def parse(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        doc = docx.Document(io.BytesIO(file_bytes))

        blocks: list[str] = []
        for para in doc.paragraphs:
            if para.text.strip():
                blocks.append(para.text)

        for table in doc.tables:
            md_rows: list[str] = []
            for i, row in enumerate(table.rows):
                cells = [cell.text.strip() for cell in row.cells]
                md_rows.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    md_rows.append("| " + " | ".join("---" for _ in cells) + " |")
            blocks.append("\n".join(md_rows))

        text = "\n\n".join(blocks)
        return ParsedDocument(
            text=text,
            pages=[{"page": 1, "text": text}],
            images=[],
            metadata={"format": "docx"},
        )


_PAGE_COMMENT_RE = re.compile(r"<!--\s*Page\s+(\d+)\s*-->")


def _detect_repeating_lines(page_texts: list[str], min_ratio: float = 0.5) -> set[str]:
    """Detect lines that appear on more than min_ratio of all pages.

    These are typically headers, footers, company info, or page decorations
    that pollute the embedding space.
    """
    if len(page_texts) < 3:
        return set()

    from collections import Counter

    line_counts: Counter[str] = Counter()
    total_pages = len(page_texts)

    for text in page_texts:
        # Deduplicate within a single page
        unique_lines = set()
        for line in text.split("\n"):
            stripped = line.strip()
            # Only consider lines with some content (skip empty lines)
            if len(stripped) > 2:
                # Normalize: collapse whitespace, ignore page numbers
                normalized = re.sub(r"\d+", "#", stripped)
                normalized = re.sub(r"\s+", " ", normalized).strip()
                if normalized not in unique_lines:
                    unique_lines.add(normalized)
                    line_counts[normalized] += 1

    threshold = total_pages * min_ratio
    repeating = set()
    for normalized, count in line_counts.items():
        if count >= threshold:
            repeating.add(normalized)

    return repeating


def _remove_header_footer(text: str, repeating: set[str]) -> str:
    """Remove lines that match the detected repeating header/footer patterns."""
    if not repeating:
        return text

    cleaned_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        normalized = re.sub(r"\d+", "#", stripped)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized in repeating:
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


class PDFHandler(BaseHandler):
    """PDF handler: MineU API as primary parser, PyMuPDF as fallback."""

    supported_extensions = [".pdf"]

    async def parse(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        if settings.USE_MINERU:
            try:
                return await self._parse_with_mineru(file_bytes, filename)
            except Exception as e:
                logger.warning("MineU fehlgeschlagen: %s – fallback auf PyMuPDF", e)
        return await self._parse_with_pymupdf(file_bytes, filename)

    # ── PyMuPDF (fallback) ───────────────────────────────────

    async def _parse_with_pymupdf(
        self, file_bytes: bytes, filename: str
    ) -> ParsedDocument:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages: list[dict] = []
        all_text_parts: list[str] = []

        # Detect repeating headers/footers across pages
        raw_texts = []
        for page in doc:
            raw_texts.append(page.get_text())
        header_footer = _detect_repeating_lines(raw_texts)

        for page_num, text in enumerate(raw_texts, start=1):
            # Remove detected headers/footers
            cleaned = _remove_header_footer(text, header_footer)
            if cleaned.strip():
                pages.append({"page": page_num, "text": cleaned})
                all_text_parts.append(f"<!-- page:{page_num} -->\n{cleaned}")

        total_pages = len(doc)
        doc.close()
        return ParsedDocument(
            text="\n\n".join(all_text_parts),
            pages=pages,
            images=[],
            metadata={"format": "pdf", "total_pages": total_pages, "parser": "pymupdf"},
        )

    # ── MineU (primary) ─────────────────────────────────────

    async def _parse_with_mineru(
        self, file_bytes: bytes, filename: str
    ) -> ParsedDocument:
        if settings.MINERU_PAGE_BY_PAGE:
            return await self._parse_mineru_page_by_page(file_bytes, filename)
        return await self._parse_mineru_single(file_bytes, filename)

    async def _parse_mineru_single(
        self, file_bytes: bytes, filename: str
    ) -> ParsedDocument:
        """Single MineU call – split resulting markdown by <!-- Page N --> comments."""
        result = await self._call_mineru(file_bytes, filename)
        return self._mineru_response_to_parsed(result, filename)

    async def _parse_mineru_page_by_page(
        self, file_bytes: bytes, filename: str
    ) -> ParsedDocument:
        """One MineU call per page – gives the most accurate page boundaries."""
        total_pages = self._count_pdf_pages(file_bytes)
        pages: list[dict] = []
        images: list[dict] = []
        all_text: list[str] = []

        for page_num in range(total_pages):
            result = await self._call_mineru(
                file_bytes, filename, start_page=page_num, end_page=page_num
            )
            markdown = result.get("markdown", "")
            if markdown.strip():
                pages.append({"page": page_num + 1, "text": markdown})
                all_text.append(markdown)

            for img in result.get("images", []):
                images.append({
                    "page": page_num + 1,
                    "base64": img.get("base64", ""),
                    "caption": img.get("caption", ""),
                })

        return ParsedDocument(
            text="\n\n".join(all_text),
            pages=pages,
            images=images,
            metadata={
                "format": "pdf",
                "total_pages": total_pages,
                "parser": "mineru",
                "mode": "page_by_page",
            },
        )

    async def _call_mineru(
        self,
        file_bytes: bytes,
        filename: str,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> dict[str, Any]:
        """Call MineU API with retry + exponential backoff."""
        last_exc: Exception | None = None
        for attempt in range(settings.MAX_RETRIES):
            try:
                return await self._do_mineru_request(
                    file_bytes, filename, start_page, end_page
                )
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < settings.MAX_RETRIES - 1:
                    wait = settings.RETRY_BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "MineU attempt %d/%d failed: %s – retry in %ds",
                        attempt + 1,
                        settings.MAX_RETRIES,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)
        raise MaxRetriesExceeded(
            f"MineU failed after {settings.MAX_RETRIES} attempts: {last_exc}"
        )

    async def _do_mineru_request(
        self,
        file_bytes: bytes,
        filename: str,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> dict[str, Any]:
        """Execute a single HTTP request to MineU."""
        data: dict[str, str] = {
            "page_by_page": str(settings.MINERU_PAGE_BY_PAGE).lower(),
        }
        if start_page is not None:
            data["start_page"] = str(start_page)
        if end_page is not None:
            data["end_page"] = str(end_page)

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{settings.MINERU_API_URL}/parse",
                files={"file": (filename, file_bytes, "application/pdf")},
                data=data,
            )
            response.raise_for_status()
            return response.json()

    # ── MineU response → ParsedDocument ──────────────────────

    def _mineru_response_to_parsed(
        self, result: dict[str, Any], filename: str
    ) -> ParsedDocument:
        """Convert a single-call MineU response to ParsedDocument.

        Splits the markdown by <!-- Page N --> comments if present.
        """
        markdown = result.get("markdown", "")
        raw_images = result.get("images", [])

        images = [
            {
                "page": img.get("page", 1),
                "base64": img.get("base64", ""),
                "caption": img.get("caption", ""),
            }
            for img in raw_images
        ]

        page_splits = self._split_by_page_comments(markdown)
        if page_splits:
            total_pages = max(p["page"] for p in page_splits)
            full_text = "\n\n".join(p["text"] for p in page_splits)
            return ParsedDocument(
                text=full_text,
                pages=page_splits,
                images=images,
                metadata={
                    "format": "pdf",
                    "total_pages": total_pages,
                    "parser": "mineru",
                    "mode": "single",
                },
            )

        # No page comments – treat as single page
        return ParsedDocument(
            text=markdown,
            pages=[{"page": 1, "text": markdown}] if markdown.strip() else [],
            images=images,
            metadata={
                "format": "pdf",
                "total_pages": 1,
                "parser": "mineru",
                "mode": "single",
            },
        )

    @staticmethod
    def _split_by_page_comments(markdown: str) -> list[dict] | None:
        """Split markdown on <!-- Page N --> markers."""
        matches = list(_PAGE_COMMENT_RE.finditer(markdown))
        if not matches:
            return None

        pages: list[dict] = []
        for i, match in enumerate(matches):
            page_num = int(match.group(1))
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            text = markdown[start:end].strip()
            if text:
                pages.append({"page": page_num, "text": text})
        return pages or None

    @staticmethod
    def _count_pdf_pages(file_bytes: bytes) -> int:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        count = len(doc)
        doc.close()
        return count


def supported_formats() -> list[str]:
    """Return sorted list of all supported file extensions."""
    return sorted(HANDLER_REGISTRY.keys())


HANDLER_REGISTRY: dict[str, BaseHandler] = {
    ".pdf": PDFHandler(),
    ".docx": DocxHandler(),
    ".csv": CsvHandler(),
    ".txt": TextHandler(),
    ".md": TextHandler(),
}

"""File-type handlers.

Each handler inherits from BaseHandler and implements
async parse(file_bytes, filename) -> ParsedDocument.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import docx
import fitz  # PyMuPDF
import httpx
from config import settings
from models import DegenerateExtractionError, MaxRetriesExceeded, ParsedDocument

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


def decode_cnc_bytes(raw: bytes) -> str:
    """Dekodiere rohe CNC-Bytes robust zu Text.

    Echte UTF-8-Exporte (mit ``Ø``) zuerst; schlägt das fehl (einzelnes
    ``0xE4`` = ``ä`` ist kein gültiges UTF-8), greift cp1252. Die
    semantische Umlaut-Normalisierung (``Frdsen`` → ``Fräsen`` etc.)
    passiert erst im data-structure-Parser (``core.cnc_parser``).
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def looks_like_cnc(text: str) -> bool:
    """Content-Sniffing für endungslose Sinumerik/MPF-Maschinendateien.

    Spiegelt ``core.cnc_parser.looks_like_cnc`` im data-structure-Service –
    bewusst dupliziert, um keine Cross-Service-Abhängigkeit einzuführen.
    """
    head = text[:2000]
    if re.search(r"%_N_.*_MPF", head):
        return True
    tokens = len(re.findall(r"\b[GMT]\d+\b|CYCLE\d+|MCALL", head))
    return tokens >= 8


class GCodeHandler(BaseHandler):
    """CNC-/G-Code-Programme (Sinumerik MPF).

    Diese Dateien kommen aus der Maschine/CAM meist OHNE Endung. Der
    Handler dekodiert robust und reicht den Text unverändert weiter; die
    eigentliche Zerlegung in Werkzeug-Operationen übernimmt der
    data-structure-Service (``POST /v1/structure/cnc``).
    """

    supported_extensions = [".mpf", ".spf", ".nc", ".cnc"]

    async def parse(self, file_bytes: bytes, filename: str) -> ParsedDocument:
        text = decode_cnc_bytes(file_bytes)
        return ParsedDocument(
            text=text,
            pages=[{"page": 1, "text": text}],
            images=[],
            metadata={"format": "cnc", "doc_type": "cnc"},
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
            # MinerU is authoritative for scanned/table PDFs. No PyMuPDF
            # fallback: a silent PyMuPDF degrade on a scanned PDF produces
            # an (almost) empty index without any error – exactly the bug
            # that made a whole document set unsearchable. Fail loudly.
            try:
                parsed = await self._parse_with_mineru(file_bytes, filename)
            except Exception as e:
                logger.error(
                    "MinerU-Extraktion fehlgeschlagen für %r: %s. Kein "
                    "PyMuPDF-Fallback (USE_MINERU=true) – Ingest abgebrochen.",
                    filename,
                    e,
                )
                raise DegenerateExtractionError(
                    f"MinerU extraction failed for {filename!r} and the "
                    f"PyMuPDF fallback is intentionally disabled while "
                    f"USE_MINERU=true: {e}"
                ) from e
        else:
            parsed = await self._parse_with_pymupdf(file_bytes, filename)

        self._guard_degenerate(parsed, filename)
        return parsed

    @staticmethod
    def _guard_degenerate(parsed: ParsedDocument, filename: str) -> None:
        """Reject a near-empty extraction instead of ingesting silence.

        A scanned/image PDF parsed without OCR yields pages with no text;
        ingesting that builds an empty, unsearchable index with zero
        warning. Surface it as a hard, actionable error.
        """
        total_pages = (parsed.metadata or {}).get("total_pages") or 0
        text_len = len((parsed.text or "").strip())
        n_text_pages = len(parsed.pages or [])
        if total_pages < 1:
            return  # unknown page count – can't judge, don't false-positive
        # Unambiguous degenerate signal: the PDF has pages but NOT ONE of
        # them produced any text (every page was empty → scanned/no OCR).
        # Kept strict so small-but-valid documents are never rejected.
        too_little = n_text_pages == 0
        # Larger PDFs: essentially no text across many pages (a scan that
        # only yielded stray artifacts). Conservative per-page floor avoids
        # false positives on short legitimate documents.
        if total_pages >= 5 and text_len < 15 * total_pages:
            too_little = True
        if too_little:
            parser = (parsed.metadata or {}).get("parser", "unknown")
            msg = (
                f"Extraktion von {filename!r} ergab fast keinen Text "
                f"({text_len} Zeichen, {n_text_pages}/{total_pages} Seiten "
                f"mit Text, Parser={parser}). Das PDF ist vermutlich "
                f"gescannt/bildbasiert. Setze USE_MINERU=true (OCR + "
                f"Tabellen), statt einen leeren Index zu erzeugen."
            )
            logger.error(msg)
            raise DegenerateExtractionError(msg)

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

    # ── MinerU (mineru-api /file_parse) ──────────────────────

    async def _parse_with_mineru(
        self, file_bytes: bytes, filename: str
    ) -> ParsedDocument:
        """Parse via the mineru-api ``POST /file_parse`` endpoint.

        The endpoint is synchronous and paginates the whole document
        itself, so we make a single call (no page-by-page loop) and
        rebuild page-anchored text + images from the structured
        ``content_list`` so downstream chunking keeps page metadata.
        """
        result = await self._call_mineru(file_bytes, filename)
        return self._mineru_response_to_parsed(result, file_bytes)

    async def _call_mineru(
        self, file_bytes: bytes, filename: str
    ) -> dict[str, Any]:
        """Call mineru-api with retry + exponential backoff."""
        last_exc: Exception | None = None
        for attempt in range(settings.MAX_RETRIES):
            try:
                return await self._do_mineru_request(file_bytes, filename)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < settings.MAX_RETRIES - 1:
                    wait = settings.RETRY_BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "MinerU attempt %d/%d failed: %s – retry in %ds",
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
        self, file_bytes: bytes, filename: str
    ) -> dict[str, Any]:
        """Single POST to mineru-api /file_parse.

        Form contract (from the server's OpenAPI): file field is ``files``
        (list), plus form flags. ``backend=pipeline`` + table/formula on
        matches the dashboard config that correctly OCR'd these scanned
        regulation PDFs.
        """
        data = {
            "backend": settings.MINERU_BACKEND,
            "parse_method": "auto",
            "formula_enable": "true",
            "table_enable": "true",
            "return_md": "true",
            "return_content_list": "true",
            "return_images": "true",
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.MINERU_TIMEOUT)
        ) as client:
            response = await client.post(
                f"{settings.MINERU_API_URL}/file_parse",
                files={"files": (filename, file_bytes, "application/pdf")},
                data=data,
            )
            response.raise_for_status()
            return response.json()

    # ── MinerU /file_parse response → ParsedDocument ─────────

    # MinerU emits tables as HTML. Embedding raw <td rowspan=…> markup
    # produces vectors dominated by tag noise rather than cell content
    # (observed: top retrieval scores collapsed to ~0.06 on verbatim
    # queries). Converting to clean pipe-markdown keeps tabular structure
    # while letting the embedding capture the actual words.
    _TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    _CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)
    _TAG_RE = re.compile(r"<[^>]+>")
    _WS_RE = re.compile(r"\s+")
    # Match a whole <table>…</table> block inside markdown so we can swap
    # it for pipe markdown without touching the surrounding prose.
    _TABLE_BLOCK_RE = re.compile(
        r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL
    )

    @classmethod
    def _convert_html_tables_in_md(cls, md: str) -> str:
        """Replace every ``<table>…</table>`` block inside the markdown
        with the pipe-table equivalent. md_content from MinerU embeds
        tables as raw HTML; leaving them in tanks the chunks' alpha
        ratio (Boilerplate-Filter drops them) and dilutes the
        embedding with markup."""
        if not md or "<table" not in md.lower():
            return md
        return cls._TABLE_BLOCK_RE.sub(
            lambda m: cls._html_table_to_pipe(m.group(0)), md
        )

    @classmethod
    def _html_table_to_pipe(cls, html: str) -> str:
        """Convert an HTML <table> to a pipe-separated markdown table.

        Falls back to plain stripped text if no <tr>/<td> are present."""
        if not html or "<" not in html:
            return cls._WS_RE.sub(" ", html).strip()
        rows: list[str] = []
        for row_html in cls._TR_RE.findall(html):
            cells = [
                cls._WS_RE.sub(" ", cls._TAG_RE.sub("", c)).strip()
                for c in cls._CELL_RE.findall(row_html)
            ]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        if rows:
            return "\n".join(rows)
        # Not a real table – strip tags and collapse whitespace.
        return cls._WS_RE.sub(" ", cls._TAG_RE.sub("", html)).strip()

    # MinerU's own page markers in md_content (e.g. "<!-- Page 14 -->").
    # When present, we skip our best-effort injection: MinerU's markers
    # are authoritative for every page, ours are only there as a
    # fallback (and our snippet-match might miss exactly the pages
    # MinerU already labelled).
    _MINERU_PAGE_MARK_RE = re.compile(
        r"<!--\s*Page\s+\d+\s*-->", re.IGNORECASE
    )

    @classmethod
    def _inject_page_anchors(cls, md: str, content_list: list[dict]) -> str:
        """Inject ``<!-- page:N -->`` anchors into ``md`` at best-effort
        positions derived from ``content_list`` page transitions.

        Each page's first content snippet is located in ``md`` via plain
        substring search; an anchor is inserted immediately before it.
        Pages whose snippet can't be located are silently skipped – the
        previous page's anchor carries forward in chunk metadata, which
        is degraded but harmless. Full ``md`` content is preserved
        verbatim regardless.

        Short-circuits when MinerU already emitted its own per-page
        markers (``<!-- Page N -->``) – those are authoritative and the
        chunker now recognises them natively."""
        if not md:
            return md
        if cls._MINERU_PAGE_MARK_RE.search(md):
            return md
        if not content_list:
            return md
        first_per_page: list[tuple[int, str]] = []  # (page_num, snippet)
        seen: set[int] = set()
        for it in content_list:
            try:
                pidx = int(it.get("page_idx", 0))
            except (TypeError, ValueError):
                pidx = 0
            if pidx in seen:
                continue
            snippet = ""
            if it.get("text"):
                snippet = str(it["text"]).strip()
            if not snippet:
                for k in ("table_caption", "img_caption", "image_caption"):
                    v = it.get(k)
                    if isinstance(v, list) and v:
                        snippet = str(v[0]).strip()
                        break
                    if v:
                        snippet = str(v).strip()
                        break
            snippet = snippet[:60].strip()
            if snippet:
                first_per_page.append((pidx + 1, snippet))
                seen.add(pidx)
        if not first_per_page:
            return md
        out: list[str] = []
        cursor = 0
        for page_num, snippet in first_per_page:
            idx = md.find(snippet, cursor)
            if idx < 0:
                continue
            if idx > cursor:
                out.append(md[cursor:idx])
            out.append(f"<!-- page:{page_num} -->\n")
            cursor = idx
        if cursor < len(md):
            out.append(md[cursor:])
        return "".join(out).lstrip()

    @classmethod
    def _content_item_text(cls, item: dict[str, Any]) -> str:
        """Render one content_list block to searchable text.

        Tables become pipe-markdown (no HTML noise). Captions and prose
        text are joined verbatim."""
        parts: list[str] = []
        if item.get("text"):
            parts.append(str(item["text"]))
        for key in ("table_caption", "img_caption", "image_caption"):
            val = item.get(key)
            if isinstance(val, list):
                parts.extend(str(v) for v in val if v)
            elif val:
                parts.append(str(val))
        if item.get("table_body"):
            parts.append(cls._html_table_to_pipe(str(item["table_body"])))
        return "\n".join(p for p in parts if p).strip()

    def _mineru_response_to_parsed(
        self, result: dict[str, Any], file_bytes: bytes
    ) -> ParsedDocument:
        """Convert the mineru-api /file_parse JSON to a ParsedDocument.

        Response shape (from mineru-api source):
            {"backend":..., "version":...,
             "results": {"<stem>": {"md_content": str,
                                     "content_list": [...],
                                     "images": {name: dataURI}}}}
        """
        results = result.get("results")
        entry: dict[str, Any] = {}
        if isinstance(results, dict) and results:
            entry = next(iter(results.values())) or {}
        md_content = (entry.get("md_content") or "").strip()
        # mineru-api returns these fields as the RAW file contents (strings),
        # not parsed JSON – content_list is the text of _content_list.json.
        content_list = entry.get("content_list") or []
        if isinstance(content_list, str):
            try:
                content_list = json.loads(content_list)
            except (ValueError, TypeError):
                content_list = []
        if not isinstance(content_list, list):
            content_list = []
        images_map = entry.get("images") or {}
        if isinstance(images_map, str):
            try:
                images_map = json.loads(images_map)
            except (ValueError, TypeError):
                images_map = {}
        if not isinstance(images_map, dict):
            images_map = {}

        # Per-page text from content_list – fed to vision/alt-text as
        # "what is on page N" context, and used as a last-resort fallback.
        by_page: dict[int, list[str]] = {}
        for it in content_list:
            try:
                pidx = int(it.get("page_idx", 0))
            except (TypeError, ValueError):
                pidx = 0
            txt = self._content_item_text(it)
            if txt:
                by_page.setdefault(pidx, []).append(txt)
        pages: list[dict] = []
        for pidx in sorted(by_page):
            ptxt = "\n\n".join(by_page[pidx]).strip()
            if ptxt:
                pages.append({"page": pidx + 1, "text": ptxt})

        # Primary text: prefer ``md_content`` (the full, dashboard-proven
        # markdown) over a content_list reconstruction. Rebuilding from
        # content_list dropped ~80 % of the document content on the live
        # corpus (9 chunks for a 23-page PDF) because content_list is
        # structurally sparse vs. md_content. Convert in-markdown HTML
        # tables to pipe so chunks aren't dominated by tag noise (which
        # also drops the alpha ratio below the boilerplate threshold).
        # Inject page anchors at best-effort positions so the chunker
        # keeps page metadata.
        if md_content:
            md_content = self._convert_html_tables_in_md(md_content)
            text = self._inject_page_anchors(md_content, content_list)
        elif pages:
            text = "\n\n".join(
                f"<!-- page:{p['page']} -->\n{p['text']}" for p in pages
            )
        else:
            text = ""

        if not pages and md_content:
            pages = [{"page": 1, "text": md_content}]

        # Images: map filename → page via content_list, strip data-URI prefix
        # so the value stays raw base64 (the existing alt-text contract).
        # Also capture the text immediately before/after each image in
        # document order so vision gets richer context than just "all text
        # on the page". Window keeps the prompt bounded.
        img_page: dict[str, int] = {}
        img_context: dict[str, tuple[str, str]] = {}
        ctx_window = 600
        for i, it in enumerate(content_list):
            p = it.get("img_path") or ""
            if not p:
                continue
            name = p.rsplit("/", 1)[-1]
            img_page[name] = int(it.get("page_idx", 0)) + 1

            before_parts: list[str] = []
            j = i - 1
            while j >= 0 and sum(len(s) for s in before_parts) < ctx_window:
                prev = content_list[j]
                if prev.get("img_path"):
                    break
                t = self._content_item_text(prev)
                if t:
                    before_parts.insert(0, t)
                j -= 1
            after_parts: list[str] = []
            j = i + 1
            while j < len(content_list) and sum(len(s) for s in after_parts) < ctx_window:
                nxt = content_list[j]
                if nxt.get("img_path"):
                    break
                t = self._content_item_text(nxt)
                if t:
                    after_parts.append(t)
                j += 1
            text_before = "\n\n".join(before_parts).strip()[-ctx_window:]
            text_after = "\n\n".join(after_parts).strip()[:ctx_window]
            img_context[name] = (text_before, text_after)

        images: list[dict] = []
        for name, data_uri in images_map.items():
            b64 = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
            # Ollama's /api/chat rejects base64 with embedded whitespace —
            # multi-line "data:image/...;base64,\nAAAA\nBBBB" from MinerU
            # silently produced "no image" on the model side.
            b64 = "".join(b64.split())
            text_before, text_after = img_context.get(name, ("", ""))
            images.append({
                "page": img_page.get(name, 1),
                "base64": b64,
                "caption": name,
                "text_before": text_before,
                "text_after": text_after,
            })

        if pages:
            total_pages = max(p["page"] for p in pages)
        else:
            try:
                total_pages = self._count_pdf_pages(file_bytes)
            except Exception:  # noqa: BLE001
                total_pages = 0

        return ParsedDocument(
            text=text,
            pages=pages,
            images=images,
            metadata={
                "format": "pdf",
                "total_pages": total_pages,
                "parser": "mineru",
                "backend": settings.MINERU_BACKEND,
            },
        )

    @staticmethod
    def _count_pdf_pages(file_bytes: bytes) -> int:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        count = len(doc)
        doc.close()
        return count


def supported_formats() -> list[str]:
    """Return sorted list of all supported file extensions."""
    return sorted(HANDLER_REGISTRY.keys())


_GCODE_HANDLER = GCodeHandler()

HANDLER_REGISTRY: dict[str, BaseHandler] = {
    ".pdf": PDFHandler(),
    ".docx": DocxHandler(),
    ".csv": CsvHandler(),
    ".txt": TextHandler(),
    ".md": TextHandler(),
    ".mpf": _GCODE_HANDLER,
    ".spf": _GCODE_HANDLER,
    ".nc": _GCODE_HANDLER,
    ".cnc": _GCODE_HANDLER,
}

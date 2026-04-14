"""Markdown chunking with page-number tracking and plugin enrichment."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import uuid4

from config import settings
from llama_index.core.node_parser import SentenceSplitter
from shared.models import Chunk, ChunkMetadata

if TYPE_CHECKING:
    from plugins.base import BasePlugin

_PAGE_ANCHOR_RE = re.compile(r"<!--\s*page:(\d+)\s*-->")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Patterns that indicate low-value boilerplate chunks
_TOC_DOTS_RE = re.compile(r"\.{5,}")  # Lines of dots (TOC filler)
_WHITESPACE_HEAVY_RE = re.compile(r"\s{3,}")  # Excessive whitespace


def _build_heading_index(markdown: str) -> list[tuple[int, int, str]]:
    """Return sorted list of (char_offset, level, title) for all markdown headings."""
    return [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in _HEADING_RE.finditer(markdown)
    ]


def _breadcrumb_for_offset(
    headings: list[tuple[int, int, str]], offset: int, max_depth: int = 3
) -> str:
    """Build 'H1 > H2 > H3' breadcrumb for the heading stack at a given offset."""
    stack: list[tuple[int, str]] = []  # (level, title)
    for h_offset, level, title in headings:
        if h_offset > offset:
            break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    return " > ".join(title for _, title in stack[:max_depth])


def _is_boilerplate(text: str) -> bool:
    """Detect chunks that are mostly boilerplate (TOC, cover pages, metadata).

    Returns True if the chunk should be skipped.
    """
    stripped = text.strip()
    if len(stripped) < 30:
        return True

    # Count meaningful vs. filler characters
    alpha_chars = sum(1 for c in stripped if c.isalpha())
    total_chars = len(stripped)

    # Less than 30% alphabetic → likely TOC dots, tables of numbers, etc.
    if total_chars > 0 and alpha_chars / total_chars < 0.3:
        return True

    # Mostly dots (table of contents filler)
    dots_removed = _TOC_DOTS_RE.sub("", stripped)
    if len(dots_removed) < len(stripped) * 0.5:
        return True

    return False


def _build_page_index(markdown: str) -> list[tuple[int, int]]:
    """Return a sorted list of (char_offset, page_number) from page anchors."""
    entries: list[tuple[int, int]] = []
    for m in _PAGE_ANCHOR_RE.finditer(markdown):
        entries.append((m.start(), int(m.group(1))))
    return entries


def _page_for_offset(page_index: list[tuple[int, int]], offset: int) -> int | None:
    """Find the page number for a given character offset."""
    if not page_index:
        return None
    page = None
    for anchor_offset, page_num in page_index:
        if anchor_offset <= offset:
            page = page_num
        else:
            break
    return page


def _strip_page_anchors(text: str) -> str:
    return _PAGE_ANCHOR_RE.sub("", text).strip()


def chunk_markdown(
    markdown: str,
    *,
    file_name: str,
    doc_type: str,
    use_case: str,
    collection: str,
    total_pages: int | None = None,
    extra: dict | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    plugin: BasePlugin | None = None,
) -> list[Chunk]:
    """Split markdown into Chunks with page-aware metadata."""
    size = chunk_size or settings.DEFAULT_CHUNK_SIZE
    overlap = chunk_overlap or settings.DEFAULT_CHUNK_OVERLAP

    page_index = _build_page_index(markdown)
    heading_index = _build_heading_index(markdown)

    splitter = SentenceSplitter(chunk_size=size, chunk_overlap=overlap)
    raw_nodes = splitter.split_text(markdown)

    chunks: list[Chunk] = []
    search_start = 0
    for node_text in raw_nodes:
        clean_text = _strip_page_anchors(node_text)
        if not clean_text:
            continue

        # Skip boilerplate chunks (TOC, cover pages, metadata-only)
        if _is_boilerplate(clean_text):
            continue

        pos = markdown.find(node_text, search_start)
        if pos == -1:
            pos = markdown.find(node_text)
        if pos != -1:
            search_start = pos + len(node_text)

        page = _page_for_offset(page_index, pos) if pos != -1 else None
        breadcrumb = _breadcrumb_for_offset(heading_index, pos) if pos != -1 else ""

        # Prepend breadcrumb + source to the chunk text so the embedding captures
        # hierarchical context ("where am I in the document?"). This meaningfully
        # improves retrieval when many chunks are topically similar.
        header_parts = [p for p in (file_name, breadcrumb) if p]
        if header_parts:
            embed_text = f"[{' | '.join(header_parts)}]\n\n{clean_text}"
        else:
            embed_text = clean_text

        chunk_id = str(uuid4())
        meta = ChunkMetadata(
            chunk_id=chunk_id,
            file_name=file_name,
            page=page,
            total_pages=total_pages,
            doc_type=doc_type,
            use_case=use_case,
            collection=collection,
            extra={**(extra or {}), "breadcrumb": breadcrumb},
        )

        if plugin is not None:
            meta = plugin.enrich_metadata(meta, clean_text, extra or {})

        chunks.append(Chunk(id=chunk_id, text=embed_text, metadata=meta))

    return chunks

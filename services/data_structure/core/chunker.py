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

# Accept both our injected lowercase "<!-- page:N -->" anchors AND MinerU's
# native "<!-- Page N -->" markers (case-insensitive, colon-or-space).
# Without this, MinerU's markers slipped through as raw text in chunks and
# pages with no other heading produced no usable chunk metadata at all.
_PAGE_ANCHOR_RE = re.compile(r"<!--\s*[Pp]age\s*[:.]?\s*(\d+)\s*-->")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Cleaning-service injects "<!-- image id:<image_id> page:<N> -->" right
# before each [Bild S.N | image_id]: <alt_text> block. We strip the anchor
# from the embedding text but use it to bind the chunk to the image.
_IMAGE_ANCHOR_RE = re.compile(
    r"<!--\s*image id:(?P<id>\S+)\s+page:(?P<page>\d+)\s*-->"
)

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

    # Markdown tables are legitimate content – the low alphabetic ratio
    # rule below would otherwise delete them (e.g. the §11 "Ersatzsignal"
    # signal table: many '|', short cells, codes like "Fa 20"). If the
    # chunk looks like a real table with enough alphabetic substance,
    # keep it regardless of the filler-character ratio.
    alpha_chars = sum(1 for c in stripped if c.isalpha())
    if stripped.count("|") >= 4 and alpha_chars >= 40:
        return False

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
    text = _IMAGE_ANCHOR_RE.sub("", text)
    return _PAGE_ANCHOR_RE.sub("", text).strip()


def _split_into_pages(markdown: str) -> list[tuple[int | None, int, str]]:
    """Split markdown into ``(page_num, base_offset, section_text)`` parts.

    Cuts at every ``<!-- Page N -->`` marker so each page is chunked
    independently. Running SentenceSplitter on the full document merges
    small sections greedily across page boundaries – verified to drop
    e.g. §11 entirely when its 1.3 kB land between two larger sections.
    """
    matches = list(_PAGE_ANCHOR_RE.finditer(markdown))
    if not matches:
        return [(None, 0, markdown)]
    sections: list[tuple[int | None, int, str]] = []
    # Anything before the first marker (cover/title) – keep as page=None.
    first = matches[0].start()
    if first > 0 and markdown[:first].strip():
        sections.append((None, 0, markdown[:first]))
    for i, m in enumerate(matches):
        page_num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        text = markdown[start:end]
        if text.strip():
            sections.append((page_num, start, text))
    return sections


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
    images: list[dict] | None = None,
) -> list[Chunk]:
    """Split markdown into Chunks with page-aware metadata.

    Page-based: the markdown is first cut at MinerU's ``<!-- Page N -->``
    markers, then SentenceSplitter runs on each page independently. This
    guarantees that every page with substantive content produces at least
    one chunk – the old whole-document split silently merged small
    sections across page boundaries.
    """
    size = chunk_size or settings.DEFAULT_CHUNK_SIZE
    overlap = chunk_overlap or settings.DEFAULT_CHUNK_OVERLAP

    # Lookup table id → image dict from the cleaning service so we can
    # attach the full {id, page, alt_text, url} payload (not just the id)
    # to every chunk that mentions the image.
    images_by_id: dict[str, dict] = {}
    # Same payloads grouped by page. A page is split into several chunks, but
    # the image anchor sits in exactly one of them — so a hit on any other
    # chunk of that page used to come back without the graphic, even though
    # the graphic belongs to the same page of the same manual. Binding at page
    # level means retrieving anywhere on the page brings its images along.
    images_by_page: dict[int, list[dict]] = {}
    for img in images or []:
        img_id = img.get("image_id") or img.get("id")
        if img_id:
            payload = {
                "id": img_id,
                "page": img.get("page"),
                "alt_text": img.get("alt_text", ""),
                "url": img.get("url", ""),
            }
            images_by_id[img_id] = payload
            try:
                page_key = int(img.get("page"))
            except (TypeError, ValueError):
                continue
            images_by_page.setdefault(page_key, []).append(payload)

    # Heading index is built once on the full markdown so breadcrumbs
    # carry across pages even when a section continues onto the next page.
    heading_index = _build_heading_index(markdown)
    # NB: SentenceSplitter measures chunk_size/overlap in TOKENS, not
    # characters. Default (256 tokens) ≈ 1000 chars for German text –
    # picked so that each German § fits in roughly one chunk.
    splitter = SentenceSplitter(chunk_size=size, chunk_overlap=overlap)

    chunks: list[Chunk] = []
    for page_num, base_offset, section_text in _split_into_pages(markdown):
        raw_nodes = splitter.split_text(section_text)
        search_start = 0
        for node_text in raw_nodes:
            # Capture image anchors BEFORE _strip_page_anchors removes them.
            chunk_image_ids = [m.group("id") for m in _IMAGE_ANCHOR_RE.finditer(node_text)]
            clean_text = _strip_page_anchors(node_text).strip()
            if not clean_text:
                continue
            if _is_boilerplate(clean_text):
                continue

            pos_in_section = section_text.find(node_text, search_start)
            if pos_in_section == -1:
                pos_in_section = section_text.find(node_text)
            if pos_in_section != -1:
                search_start = pos_in_section + len(node_text)
                global_offset = base_offset + pos_in_section
            else:
                global_offset = base_offset

            breadcrumb = _breadcrumb_for_offset(heading_index, global_offset)

            # Prepend breadcrumb + source to the chunk text so the embedding
            # captures hierarchical context ("where am I in the document?").
            header_parts = [p for p in (file_name, breadcrumb) if p]
            if header_parts:
                embed_text = f"[{' | '.join(header_parts)}]\n\n{clean_text}"
            else:
                embed_text = clean_text

            chunk_id = str(uuid4())
            # Anchored images first — those sit in this very chunk — then the
            # rest of the page's images. Order matters downstream: the answer
            # side offers them to the model in this sequence, so the most
            # directly related graphic leads.
            chunk_images: list[dict] = []
            seen_image_ids: set[str] = set()
            for img_id in chunk_image_ids:
                payload = images_by_id.get(img_id)
                if payload and img_id not in seen_image_ids:
                    seen_image_ids.add(img_id)
                    chunk_images.append(payload)
            for payload in images_by_page.get(page_num, []):
                img_id = payload["id"]
                if img_id not in seen_image_ids:
                    seen_image_ids.add(img_id)
                    chunk_images.append(payload)
            # Keys derived from *this chunk's* position in the document. No
            # plugin can reconstruct them — they only exist here.
            chunk_owned: dict = {"breadcrumb": breadcrumb}
            if chunk_images:
                chunk_owned["images"] = chunk_images
            meta_extra = {**(extra or {}), **chunk_owned}
            meta = ChunkMetadata(
                chunk_id=chunk_id,
                file_name=file_name,
                page=page_num,
                total_pages=total_pages,
                doc_type=doc_type,
                use_case=use_case,
                collection=collection,
                extra=meta_extra,
            )

            if plugin is not None:
                meta = plugin.enrich_metadata(meta, clean_text, extra or {})
                # Every plugin rebuilds extra as {**raw_meta, ...} and thereby
                # drops whatever the chunker put there — raw_meta is the
                # document-level dict, not this chunk's. That silently deleted
                # the image bindings and breadcrumbs on the way to the index.
                # Re-applying here keeps the invariant regardless of what a
                # plugin does, instead of relying on every plugin to remember.
                meta.extra = {**(meta.extra or {}), **chunk_owned}

            chunks.append(Chunk(id=chunk_id, text=embed_text, metadata=meta))

    return chunks

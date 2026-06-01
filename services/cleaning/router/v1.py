from __future__ import annotations

import base64
import json
import os
import re
import unicodedata

from core.batch import process_batch
from core.cleaner import clean
from core.handlers import supported_formats
from core.pii import remove_pii
from config import settings
from core.storage import (
    delete_document_files,
    get_absolute_path,
    get_image_absolute_path,
    list_document_images,
    store_image,
    store_original,
    write_image_debug_txt,
    write_image_metadata,
)
from core.vision import enrich_images_with_alt_text
from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/formats")
async def formats() -> list[str]:
    """Return all supported file extensions."""
    return supported_formats()


@router.post("/clean")
async def clean_file(
    request: Request,
    file: UploadFile,
    config: str = Form("{}"),
):
    """Clean a single uploaded document and return structured markdown."""
    request_id = getattr(request.state, "request_id", "unknown")
    cfg = json.loads(config)
    extract_images = cfg.get("extract_images", True)
    pii_removal = cfg.get("pii_removal", False)

    filename = file.filename or "unknown"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in supported_formats():
        return JSONResponse(
            status_code=400,
            content={
                "error": "unsupported_format",
                "detail": f"Format {ext!r} not supported. "
                f"Supported: {', '.join(supported_formats())}",
                "request_id": request_id,
                "service": "cleaning-service",
            },
        )

    file_bytes = await file.read()

    try:
        doc = await clean(file_bytes, filename)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": "parsing_failed",
                "detail": str(exc),
                "request_id": request_id,
                "service": "cleaning-service",
            },
        )

    markdown = doc.text
    pages = doc.pages
    images = doc.images if extract_images else []

    if pii_removal:
        markdown = remove_pii(markdown)
        pages = [{**p, "text": remove_pii(p["text"])} for p in pages]

    # Use-case is consumed by both alt-text generation (for per-usecase
    # vision provider/model/prompt) and original-file storage below.
    use_case = cfg.get("use_case", "")
    use_case_dir = use_case or "_unsorted"

    # Store original file for later retrieval — and so the file_hash is
    # available as the prefix for image storage below.
    file_hash, stored_path = store_original(file_bytes, use_case_dir, filename)

    # Generate alt-text + persist each image to disk so the answer-side
    # can render it inline. The image_id is woven into the markdown anchor
    # so the chunker can pick it up later.
    if images:
        images = await enrich_images_with_alt_text(images, pages, use_case=use_case)
        per_page_idx: dict[int, int] = {}
        enriched: list[dict] = []
        alt_blocks: list[str] = []
        for img in images:
            page_num = int(img.get("page") or 1)
            idx = per_page_idx.get(page_num, 0)
            per_page_idx[page_num] = idx + 1
            try:
                image_bytes = base64.b64decode(img.get("base64") or "")
            except (ValueError, TypeError):
                image_bytes = b""
            if not image_bytes:
                enriched.append(img)
                continue
            image_id, rel_path, _ext = store_image(
                image_bytes=image_bytes,
                use_case=use_case_dir,
                file_hash=file_hash,
                page=page_num,
                idx=idx,
            )
            url = f"/api/images/{use_case_dir}/{image_id}"
            enriched.append({
                **img,
                "image_id": image_id,
                "stored_path": rel_path,
                "url": url,
            })
            alt = img.get("alt_text", "")
            # JSON sidecar is the gallery index — always write it so the
            # /documents/{hash}/images endpoint can recover the alt-text
            # without re-running vision on retrieval.
            write_image_metadata(
                use_case=use_case_dir,
                image_id=image_id,
                page=page_num,
                alt_text=alt,
                text_before=img.get("text_before", ""),
                text_after=img.get("text_after", ""),
            )
            if settings.DEBUG_IMAGE_CAPTIONS:
                write_image_debug_txt(
                    use_case=use_case_dir,
                    image_id=image_id,
                    page=page_num,
                    alt_text=alt,
                    text_before=img.get("text_before", ""),
                    text_after=img.get("text_after", ""),
                )
            anchor_block = (
                f"<!-- image id:{image_id} page:{page_num} -->\n"
                f"[Bild S.{page_num} | {image_id}]: {alt}"
                if alt else
                f"<!-- image id:{image_id} page:{page_num} -->"
            )
            # MinerU emittiert das Bild im md_content als ![…](images/<name>).
            # Wir ersetzen den ![]()-Tag inline durch unseren Anchor, damit
            # der Chunker das Bild dem Chunk zuordnet, in dem es im Dokument
            # tatsächlich steht — sonst landen alle Bild-Anchors in einem
            # Anhang-Chunk und werden nie zusammen mit dem relevanten Inhalt
            # zitiert.
            mineru_name = img.get("caption", "")
            placed_inline = False
            if mineru_name:
                pattern = re.compile(
                    r"!\[[^\]]*\]\([^)]*?"
                    + re.escape(mineru_name)
                    + r"[^)]*\)"
                )
                # Nur die erste Stelle ersetzen; weitere Vorkommen werden
                # vom Chunker als Wiederholungen behandelt (harmlos).
                new_markdown, n = pattern.subn(anchor_block, markdown, count=1)
                if n > 0:
                    markdown = new_markdown
                    placed_inline = True
            if not placed_inline:
                # Fallback 1: kein ![]()-Tag im Markdown (typisch für
                # MinerU-Tabellen-Screenshots, die im md_content als
                # Pipe-Table-Text erscheinen). Anchor hinter den Page-
                # Marker der zugehörigen Seite klemmen — dann landet er
                # zumindest in einem Chunk derselben Seite.
                page_marker_re = re.compile(
                    r"(<!--\s*page[:\s]+" + str(page_num) + r"\s*-->)",
                    re.IGNORECASE,
                )
                m = page_marker_re.search(markdown)
                if m:
                    insert_at = m.end()
                    markdown = (
                        markdown[:insert_at]
                        + "\n\n" + anchor_block
                        + markdown[insert_at:]
                    )
                    placed_inline = True
            if not placed_inline and alt:
                # Fallback 2: weder ![]()-Tag noch passender Page-Marker
                # gefunden — am Dokumentende anhängen, damit zumindest
                # die Bild-Beschreibung im Embedding-Pool landet.
                alt_blocks.append(anchor_block)
        images = enriched
        if alt_blocks:
            markdown = markdown + "\n\n" + "\n\n".join(alt_blocks)

    return {
        "status": "ok",
        "request_id": request_id,
        "markdown": markdown,
        "images": images,
        "pages": pages,
        "metadata": {
            "file_name": filename,
            "total_pages": doc.metadata.get("total_pages", len(pages)),
            "doc_type": doc.metadata.get("format", ext.lstrip(".")),
            "used_mineru": doc.metadata.get("parser") == "mineru",
            "file_hash": file_hash,
            "stored_path": stored_path,
        },
    }


_INLINE_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
}


_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@router.get("/images/{use_case}/{image_id}")
async def serve_image(use_case: str, image_id: str, request: Request):
    """Serve a single MinerU-extracted image for inline rendering in chat."""
    request_id = getattr(request.state, "request_id", "unknown")
    abs_path = get_image_absolute_path(use_case, image_id)
    if not abs_path:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "detail": f"Image not found: {image_id}",
                "request_id": request_id,
                "service": "cleaning-service",
            },
        )
    ext = os.path.splitext(abs_path)[1].lower()
    media_type = _IMAGE_MIME_TYPES.get(ext, "application/octet-stream")
    return FileResponse(abs_path, media_type=media_type)


@router.delete("/documents/{use_case}/{file_hash}")
async def delete_document(
    use_case: str,
    file_hash: str,
    request: Request,
    stored_path: str = "",
):
    """Remove original file + all extracted images/sidecars for one doc."""
    request_id = getattr(request.state, "request_id", "unknown")
    result = delete_document_files(use_case, file_hash, stored_path or None)
    return {
        "status": "ok",
        "removed": result.get("removed", []),
        "request_id": request_id,
    }


@router.get("/documents/{use_case}/{file_hash}/images")
async def list_document_image_gallery(
    use_case: str, file_hash: str, request: Request
):
    """Return every persisted MinerU image for one ingested document
    together with its generated alt-text + surrounding-text context."""
    request_id = getattr(request.state, "request_id", "unknown")
    images = list_document_images(use_case, file_hash)
    return {
        "status": "ok",
        "request_id": request_id,
        "use_case": use_case,
        "file_hash": file_hash,
        "images": images,
    }


@router.get("/documents/{path:path}")
async def serve_document(path: str, request: Request):
    """Serve an original stored document for inline preview."""
    request_id = getattr(request.state, "request_id", "unknown")
    abs_path = get_absolute_path(path)
    if not abs_path:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "detail": f"Document not found: {path}",
                "request_id": request_id,
                "service": "cleaning-service",
            },
        )
    filename = os.path.basename(abs_path)
    if "_" in filename:
        display_name = filename.split("_", 1)[1]
    else:
        display_name = filename
    # macOS-uploaded names arrive in NFD (e.g. "a" + U+0308); collapse to
    # NFC so the Content-Disposition header is a single codepoint per
    # glyph — then let Starlette percent-encode for RFC 5987.
    display_name = unicodedata.normalize("NFC", display_name)

    ext = os.path.splitext(display_name)[1].lower()
    media_type = _INLINE_MIME_TYPES.get(ext)

    if media_type:
        # Serve inline (browser renders PDF, text, etc.)
        return FileResponse(
            abs_path,
            media_type=media_type,
            filename=display_name,
            content_disposition_type="inline",
        )
    # Fallback: download for unknown types
    return FileResponse(abs_path, filename=display_name)


@router.post("/clean/batch")
async def clean_batch(
    request: Request,
    files: list[UploadFile],
    config: str = Form("{}"),
):
    """Clean multiple uploaded documents in parallel."""
    request_id = getattr(request.state, "request_id", "unknown")
    cfg = json.loads(config)
    extract_images = cfg.get("extract_images", True)
    pii_removal = cfg.get("pii_removal", False)

    file_data = []
    for f in files:
        data = await f.read()
        file_data.append((f.filename or "unknown", data))

    batch = await process_batch(
        file_data,
        extract_images=extract_images,
        pii_removal=pii_removal,
    )

    return {
        "status": "ok",
        "request_id": request_id,
        "total": batch.total,
        "success": batch.success,
        "failed": batch.failed,
        "results": [
            {
                "file_name": r.file_name,
                "status": r.status,
                "markdown": r.markdown,
                "metadata": r.metadata,
            }
            for r in batch.results
        ],
        "errors": [
            {
                "file_name": e.file_name,
                "error": e.error,
                "detail": e.detail,
            }
            for e in batch.errors
        ],
    }

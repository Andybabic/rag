from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import unicodedata

from core.batch import process_batch
from core.cleaner import clean
from core.handlers import supported_formats
from core.image_jobs import get_progress, run_alt_text_job
from core.pii import remove_pii
from core.storage import (
    delete_document_files,
    get_absolute_path,
    get_image_absolute_path,
    list_document_images,
    store_image,
    store_original,
    write_image_metadata,
)
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

    import logging

    logger = logging.getLogger(__name__)
    logger.info(
        "clean start: %r (%d bytes), use_case=%r", filename, len(file_bytes),
        cfg.get("use_case", ""),
    )
    try:
        doc = await clean(file_bytes, filename)
    except Exception as exc:
        logger.error("clean failed for %r: %s", filename, exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": "parsing_failed",
                "detail": str(exc),
                "request_id": request_id,
                "service": "cleaning-service",
            },
        )
    logger.info(
        "clean parsed %r: %s page(s), %d image(s), parser=%s",
        filename,
        (doc.metadata or {}).get("total_pages", len(doc.pages or [])),
        len(doc.images or []),
        (doc.metadata or {}).get("parser", "?"),
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

    # Persist each image to disk and weave its anchor into the markdown NOW
    # (fast), so the clean response returns quickly. The slow vision alt-text
    # generation is handed off to a background job (see core.image_jobs); the
    # frontend polls /v1/images/progress/{file_hash} to show "X/Y Bilder
    # interpretiert" while it runs.
    image_count = 0
    if images:
        page_text: dict[int, str] = {}
        for p in pages:
            page_text.setdefault(int(p.get("page", 1)), p.get("text", ""))

        per_page_idx: dict[int, int] = {}
        enriched: list[dict] = []
        job_items: list[dict] = []
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
            text_before = img.get("text_before", "")
            text_after = img.get("text_after", "")
            # Placeholder sidecar (alt-text filled in by the background job) so
            # the gallery already lists the image during processing.
            write_image_metadata(
                use_case=use_case_dir,
                image_id=image_id,
                page=page_num,
                alt_text="",
                text_before=text_before,
                text_after=text_after,
            )
            # Anchor carries only the id/page (no description yet). The chunker
            # associates the image with its location; the answer-side reads the
            # description from the sidecar once the background job filled it in.
            anchor_block = f"<!-- image id:{image_id} page:{page_num} -->"
            # Replace MinerU's ![…](images/<name>) tag inline so the image lands
            # in the chunk where it actually appears. Function replacement — a
            # plain string would misread backslashes as group escapes.
            mineru_name = img.get("caption", "")
            placed_inline = False
            if mineru_name:
                pattern = re.compile(
                    r"!\[[^\]]*\]\([^)]*?" + re.escape(mineru_name) + r"[^)]*\)"
                )
                new_markdown, n = pattern.subn(lambda _m: anchor_block, markdown, count=1)
                if n > 0:
                    markdown = new_markdown
                    placed_inline = True
            if not placed_inline:
                # Fallback: no ![]() tag (e.g. MinerU table screenshots) — clamp
                # the anchor behind the matching page marker.
                page_marker_re = re.compile(
                    r"(<!--\s*page[:\s]+" + str(page_num) + r"\s*-->)",
                    re.IGNORECASE,
                )
                m = page_marker_re.search(markdown)
                if m:
                    insert_at = m.end()
                    markdown = markdown[:insert_at] + "\n\n" + anchor_block + markdown[insert_at:]

            context = ""
            if not (text_before or text_after):
                context = page_text.get(page_num, "")[:300]
            job_items.append({
                "image_id": image_id,
                "page": page_num,
                "text_before": text_before,
                "text_after": text_after,
                "context": context,
            })
        images = enriched
        image_count = len(job_items)

        # Kick off alt-text generation in the background (non-blocking). The
        # clean response returns immediately; progress is polled separately.
        if job_items:
            asyncio.create_task(
                run_alt_text_job(file_hash, use_case_dir, use_case, job_items)
            )

    return {
        "status": "ok",
        "request_id": request_id,
        "markdown": markdown,
        "images": images,
        "pages": pages,
        # How many images are being described in the background — the frontend
        # polls /v1/images/progress/{file_hash} to show live progress.
        "image_count": image_count,
        "metadata": {
            "file_name": filename,
            "total_pages": doc.metadata.get("total_pages", len(pages)),
            "doc_type": doc.metadata.get("format", ext.lstrip(".")),
            "used_mineru": doc.metadata.get("parser") == "mineru",
            "file_hash": file_hash,
            "stored_path": stored_path,
        },
    }


@router.get("/images/progress/{file_hash}")
async def image_progress(file_hash: str):
    """Background alt-text progress for a document (for the upload UI)."""
    return get_progress(file_hash)


@router.get("/images/status/{use_case}/{file_hash}")
async def image_status(use_case: str, file_hash: str):
    """Durable image-description status for a document, derived from the stored
    sidecars (survives reloads/restarts). Used by the documents overview.
    """
    imgs = list_document_images(use_case, file_hash)
    total = len(imgs)
    described = sum(1 for i in imgs if (i.get("alt_text") or "").strip())
    job = get_progress(file_hash)
    return {
        "file_hash": file_hash,
        "total": total,
        "described": described,
        "pending": total - described,
        "job_status": job.get("status", "unknown"),  # running | done | unknown
    }


@router.post("/images/regenerate/{use_case}/{file_hash}")
async def regenerate_images(use_case: str, file_hash: str):
    """(Re-)generate alt-text for images of a document that have none yet.

    Lets the user recover missing descriptions (e.g. after fixing a vision
    config) WITHOUT re-uploading and re-parsing the whole PDF.
    """
    imgs = list_document_images(use_case, file_hash)
    pending = [i for i in imgs if not (i.get("alt_text") or "").strip()]
    if not pending:
        return {"status": "nothing_to_do", "total": len(imgs), "pending": 0}
    items = [
        {
            "image_id": i["image_id"],
            "page": i["page"],
            "text_before": i.get("text_before", ""),
            "text_after": i.get("text_after", ""),
            "context": "",
        }
        for i in pending
    ]
    asyncio.create_task(run_alt_text_job(file_hash, use_case, use_case, items))
    return {"status": "started", "pending": len(items), "total": len(imgs)}


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

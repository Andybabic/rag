from __future__ import annotations

import json
import os

from core.batch import process_batch
from core.cleaner import clean
from core.handlers import supported_formats
from core.pii import remove_pii
from core.storage import get_absolute_path, store_original
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

    # Generate alt-text for images via multimodal LLM
    if images:
        images = await enrich_images_with_alt_text(images, pages)
        # Append image descriptions to the markdown so they flow into RAG
        alt_blocks = []
        for img in images:
            alt = img.get("alt_text", "")
            if alt:
                pg = img.get("page", "?")
                alt_blocks.append(f"<!-- image page:{pg} -->\n[Bild S.{pg}]: {alt}")
        if alt_blocks:
            markdown = markdown + "\n\n" + "\n\n".join(alt_blocks)

    # Store original file for later retrieval
    use_case = cfg.get("use_case", "")
    file_hash, stored_path = store_original(file_bytes, use_case or "_unsorted", filename)

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

    ext = os.path.splitext(display_name)[1].lower()
    media_type = _INLINE_MIME_TYPES.get(ext)

    if media_type:
        # Serve inline (browser renders PDF, text, etc.)
        return FileResponse(
            abs_path,
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{display_name}"'},
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

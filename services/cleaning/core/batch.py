"""Batch processing with bounded concurrency."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import settings

from core.cleaner import clean
from core.pii import remove_pii

logger = logging.getLogger(__name__)


@dataclass
class BatchFileResult:
    file_name: str
    status: str
    markdown: str = ""
    pages: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class BatchFileError:
    file_name: str
    error: str
    detail: str


@dataclass
class BatchResult:
    total: int
    success: int
    failed: int
    results: list[BatchFileResult]
    errors: list[BatchFileError]


async def process_batch(
    files: list[tuple[str, bytes]],
    *,
    extract_images: bool = True,
    pii_removal: bool = False,
) -> BatchResult:
    """Process multiple files concurrently with bounded parallelism."""
    sem = asyncio.Semaphore(settings.BATCH_PARSE_WORKERS)
    results: list[BatchFileResult] = []
    errors: list[BatchFileError] = []

    async def _process_one(filename: str, data: bytes) -> None:
        async with sem:
            try:
                doc = await clean(data, filename)
                markdown = doc.text
                images = doc.images if extract_images else []

                if pii_removal:
                    markdown = remove_pii(markdown)
                    for page in doc.pages:
                        page["text"] = remove_pii(page["text"])

                results.append(
                    BatchFileResult(
                        file_name=filename,
                        status="ok",
                        markdown=markdown,
                        pages=doc.pages,
                        images=images,
                        metadata=doc.metadata,
                    )
                )
            except Exception as exc:
                err = BatchFileError(
                    file_name=filename,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                errors.append(err)
                _log_batch_error(err)

    tasks = [_process_one(name, data) for name, data in files]
    await asyncio.gather(*tasks)

    return BatchResult(
        total=len(files),
        success=len(results),
        failed=len(errors),
        results=results,
        errors=errors,
    )


def _log_batch_error(err: BatchFileError) -> None:
    log_dir = os.path.join(settings.LOG_DIR, "cleaning")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "batch_errors.jsonl")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "file_name": err.file_name,
        "error": err.error,
        "detail": err.detail,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.error("Batch error for %s: %s – %s", err.file_name, err.error, err.detail)

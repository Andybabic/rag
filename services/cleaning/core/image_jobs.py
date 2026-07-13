"""Background alt-text generation with queryable progress.

The synchronous ``/v1/clean`` path stores the raw images and weaves their
anchors immediately, then hands the (slow) vision alt-text generation off to a
background task tracked here. The frontend polls ``/v1/images/progress/{hash}``
to show e.g. "14/44 Bilder interpretiert" while it runs.

Progress lives in-memory (single uvicorn process): it's transient status, not
data — a restart just loses the progress view, the stored images/anchors are
already persisted.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from config import settings
from core.storage import get_image_absolute_path, write_image_metadata
from core.vision import generate_alt_text

logger = logging.getLogger(__name__)

# file_hash -> {total, done, failed, status, use_case, ts}
_progress: dict[str, dict] = {}
_MAX_JOBS = 50  # cap the registry so it can't grow unbounded


def get_progress(file_hash: str) -> dict:
    """Return the progress record for a document, or an 'unknown' stub."""
    return _progress.get(file_hash) or {
        "file_hash": file_hash,
        "total": 0,
        "done": 0,
        "failed": 0,
        "status": "unknown",
    }


def _evict_if_needed() -> None:
    if len(_progress) <= _MAX_JOBS:
        return
    # Drop the oldest finished entries first, then oldest overall.
    victims = sorted(_progress.items(), key=lambda kv: kv[1].get("ts", 0))
    for fh, _ in victims[: len(_progress) - _MAX_JOBS]:
        _progress.pop(fh, None)


def start_job(file_hash: str, total: int, use_case: str) -> None:
    _evict_if_needed()
    _progress[file_hash] = {
        "file_hash": file_hash,
        "total": total,
        "done": 0,
        "failed": 0,
        "status": "running",
        "use_case": use_case,
        "ts": time.time(),
    }


async def run_alt_text_job(
    file_hash: str,
    use_case_dir: str,
    use_case: str,
    items: list[dict],
) -> None:
    """Generate alt-text for each already-stored image and update its sidecar.

    ``items`` entries: ``{image_id, page, text_before, text_after, context}``.
    Image bytes are read back from disk (kept out of memory). Best-effort: a
    failed image is counted but never aborts the job.
    """
    total = len(items)
    start_job(file_hash, total, use_case_dir)
    rec = _progress[file_hash]
    rec["described"] = 0  # non-empty alt-texts actually produced
    logger.info(
        "alt-text job started: %d image(s) for %s (concurrency=%d)",
        total, file_hash[:12], settings.VISION_CONCURRENCY,
    )
    sem = asyncio.Semaphore(max(1, settings.VISION_CONCURRENCY))
    step = max(1, total // 10)

    async def _one(it: dict) -> None:
        image_id = it["image_id"]
        alt = ""
        try:
            path = get_image_absolute_path(use_case_dir, image_id)
            b64 = ""
            if path:
                with open(path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
            if b64:
                async with sem:
                    alt = await generate_alt_text(
                        b64,
                        it.get("context", ""),
                        use_case=use_case,
                        text_before=it.get("text_before", ""),
                        text_after=it.get("text_after", ""),
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort per image
            logger.warning("alt-text failed for %s: %s", image_id, exc)
        # generate_alt_text swallows provider errors and returns "" — count an
        # empty result as "not described" so a misconfigured vision model (e.g.
        # a 403) shows up as failures instead of a misleading 0.
        if (alt or "").strip():
            rec["described"] += 1
        else:
            rec["failed"] += 1
        # Persist the description into the sidecar so the answer-side gallery
        # picks it up without re-running vision.
        try:
            write_image_metadata(
                use_case=use_case_dir,
                image_id=image_id,
                page=int(it.get("page", 1)),
                alt_text=alt,
                text_before=it.get("text_before", ""),
                text_after=it.get("text_after", ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sidecar update failed for %s: %s", image_id, exc)
        rec["done"] += 1
        if rec["done"] % step == 0 or rec["done"] == total:
            logger.info(
                "alt-text progress %s: %d/%d (%d failed)",
                file_hash[:12], rec["done"], total, rec["failed"],
            )

    try:
        await asyncio.gather(*(_one(it) for it in items))
    finally:
        rec["status"] = "done"
        rec["ts"] = time.time()
        logger.info(
            "alt-text job done: %s %d/%d (%d failed)",
            file_hash[:12], rec["done"], total, rec["failed"],
        )

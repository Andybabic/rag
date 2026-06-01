"""Persistent file storage for original uploaded documents."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from config import settings


def compute_file_hash(file_bytes: bytes) -> str:
    """Compute SHA-256 hash of file contents."""
    return hashlib.sha256(file_bytes).hexdigest()


def store_original(file_bytes: bytes, use_case: str, filename: str) -> tuple[str, str]:
    """Store the original file and return (file_hash, relative_path).

    Directory layout: <storage_dir>/<use_case>/<hash[:2]>/<hash>_<filename>
    """
    file_hash = compute_file_hash(file_bytes)
    prefix = file_hash[:2]
    safe_name = filename.replace("/", "_").replace("\\", "_")
    stored_name = f"{file_hash[:12]}_{safe_name}"

    rel_dir = os.path.join(use_case, prefix)
    abs_dir = os.path.join(settings.FILE_STORAGE_DIR, rel_dir)
    Path(abs_dir).mkdir(parents=True, exist_ok=True)

    rel_path = os.path.join(rel_dir, stored_name)
    abs_path = os.path.join(settings.FILE_STORAGE_DIR, rel_path)

    if not os.path.exists(abs_path):
        with open(abs_path, "wb") as f:
            f.write(file_bytes)

    return file_hash, rel_path


def get_absolute_path(rel_path: str) -> str | None:
    """Resolve a relative storage path to its absolute path, if it exists."""
    abs_path = os.path.join(settings.FILE_STORAGE_DIR, rel_path)
    return abs_path if os.path.exists(abs_path) else None


# ── Images extracted by MinerU ──────────────────────────────────────────────

def _image_extension(image_bytes: bytes) -> str:
    """Pick a file extension from the leading magic bytes."""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def make_image_id(file_hash: str, page: int, idx: int) -> str:
    """Deterministic ID for a MinerU image — re-uploading the same PDF
    yields the same IDs so chunks/embeddings stay stable.
    """
    return f"img_{file_hash[:12]}_p{int(page):03d}_i{int(idx):02d}"


def store_image(
    *,
    image_bytes: bytes,
    use_case: str,
    file_hash: str,
    page: int,
    idx: int,
) -> tuple[str, str, str]:
    """Persist a single page-image and return (image_id, rel_path, extension).

    Layout: <storage_dir>/<use_case>/<hash[:2]>/images/<image_id>.<ext>
    Identical IDs / paths across re-uploads of the same PDF.
    """
    image_id = make_image_id(file_hash, page, idx)
    ext = _image_extension(image_bytes)

    rel_dir = os.path.join(use_case, file_hash[:2], "images")
    abs_dir = os.path.join(settings.FILE_STORAGE_DIR, rel_dir)
    Path(abs_dir).mkdir(parents=True, exist_ok=True)

    rel_path = os.path.join(rel_dir, f"{image_id}.{ext}")
    abs_path = os.path.join(settings.FILE_STORAGE_DIR, rel_path)

    if not os.path.exists(abs_path):
        with open(abs_path, "wb") as f:
            f.write(image_bytes)

    return image_id, rel_path, ext


def write_image_metadata(
    *,
    use_case: str,
    image_id: str,
    page: int,
    alt_text: str,
    text_before: str = "",
    text_after: str = "",
) -> str | None:
    """Persist a JSON sidecar next to the stored image with the alt-text +
    surrounding-text context. Always written, even when the debug .txt is
    disabled — this file is the index the gallery endpoint reads.

    Layout: <image_id>.json next to <image_id>.<ext>.
    Returns the absolute path written, or None when the image isn't on
    disk yet.
    """
    img_path = get_image_absolute_path(use_case, image_id)
    if not img_path:
        return None
    meta_path = os.path.splitext(img_path)[0] + ".json"
    payload = {
        "image_id": image_id,
        "page": int(page),
        "alt_text": alt_text or "",
        "text_before": text_before or "",
        "text_after": text_after or "",
    }
    Path(meta_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta_path


def list_document_images(use_case: str, file_hash: str) -> list[dict]:
    """Return the gallery entries for one uploaded document.

    Walks ``<storage>/<use_case>/<hash[:2]>/images/`` and picks images
    whose ID encodes the requested ``file_hash[:12]`` (deterministic from
    :func:`make_image_id`). For each image, loads the JSON metadata
    sidecar — falls back to an empty alt-text when none exists (e.g.
    documents ingested before the sidecar was introduced).
    """
    if len(file_hash) < 12:
        return []
    hash12 = file_hash[:12]
    img_dir = os.path.join(
        settings.FILE_STORAGE_DIR, use_case, hash12[:2], "images"
    )
    if not os.path.isdir(img_dir):
        return []
    out: list[dict] = []
    for fname in sorted(os.listdir(img_dir)):
        stem, ext = os.path.splitext(fname)
        ext = ext.lower().lstrip(".")
        if ext not in {"jpg", "jpeg", "png", "gif", "webp"}:
            continue
        if not stem.startswith(f"img_{hash12}_"):
            continue
        meta_path = os.path.join(img_dir, stem + ".json")
        meta: dict = {}
        if os.path.isfile(meta_path):
            try:
                meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta = {}
        # ``img_<hash12>_p<page>_i<idx>`` — fall back to parsing the ID
        # when the sidecar is missing so old uploads still show a page.
        page = meta.get("page")
        if page is None:
            parts = stem.split("_")
            page_part = next((p for p in parts if p.startswith("p")), "")
            try:
                page = int(page_part[1:]) if page_part else 1
            except ValueError:
                page = 1
        out.append({
            "image_id": stem,
            "page": int(page),
            "alt_text": meta.get("alt_text", ""),
            "text_before": meta.get("text_before", ""),
            "text_after": meta.get("text_after", ""),
            "url": f"/api/images/{use_case}/{stem}",
        })
    out.sort(key=lambda x: (x["page"], x["image_id"]))
    return out


def write_image_debug_txt(
    *,
    use_case: str,
    image_id: str,
    page: int,
    alt_text: str,
    text_before: str = "",
    text_after: str = "",
) -> str | None:
    """Persist a debug .txt next to a stored MinerU image.

    Layout mirrors ``store_image``: same directory, same basename, ``.txt``
    extension. Returns the absolute path written, or None when the image
    itself isn't on disk.
    """
    img_path = get_image_absolute_path(use_case, image_id)
    if not img_path:
        return None
    txt_path = os.path.splitext(img_path)[0] + ".txt"
    content = (
        f"image_id: {image_id}\n"
        f"page: {page}\n"
        f"\n"
        f"── Vision description ────────────────\n"
        f"{alt_text or '(empty)'}\n"
        f"\n"
        f"── Text davor ────────────────────────\n"
        f"{text_before or '(none)'}\n"
        f"\n"
        f"── Text danach ───────────────────────\n"
        f"{text_after or '(none)'}\n"
    )
    Path(txt_path).write_text(content, encoding="utf-8")
    return txt_path


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def delete_document_files(
    use_case: str, file_hash: str, stored_path: str | None = None
) -> dict:
    """Remove the original upload + every persisted image and sidecar that
    belongs to one ingested document.

    Idempotent: missing files are skipped silently — the caller's job is to
    reach an end state, not to verify each file existed.

    Layout reference:
        <storage>/<stored_path>                             — original
        <storage>/<use_case>/<hash[:2]>/images/img_<hash[:12]>_*.{jpg,json,txt}
    """
    removed: list[str] = []
    base = settings.FILE_STORAGE_DIR

    if stored_path:
        abs_path = os.path.join(base, stored_path)
        if os.path.isfile(abs_path):
            try:
                os.remove(abs_path)
                removed.append(stored_path)
            except OSError:
                pass

    if len(file_hash) >= 12:
        hash12 = file_hash[:12]
        img_dir = os.path.join(base, use_case, hash12[:2], "images")
        if os.path.isdir(img_dir):
            prefix = f"img_{hash12}_"
            for fname in list(os.listdir(img_dir)):
                if not fname.startswith(prefix):
                    continue
                fpath = os.path.join(img_dir, fname)
                try:
                    os.remove(fpath)
                    removed.append(
                        os.path.join(use_case, hash12[:2], "images", fname)
                    )
                except OSError:
                    pass
            # Wenn der images-Ordner jetzt leer ist, mit weg
            try:
                if not os.listdir(img_dir):
                    os.rmdir(img_dir)
            except OSError:
                pass

    return {"removed": removed}


def get_image_absolute_path(use_case: str, image_id: str) -> str | None:
    """Resolve (use_case, image_id) to the on-disk path of the image.

    The image_id encodes the hash prefix (chars 4..16 are ``hash[:12]``),
    so we can rebuild the directory directly — no separate index needed.
    Filters by image extension so the JSON/TXT sidecars that now live
    alongside each image aren't returned as the image binary.
    """
    if not image_id.startswith("img_") or len(image_id) < 8:
        return None
    hash12 = image_id[4:16]
    if len(hash12) != 12:
        return None
    img_dir = os.path.join(
        settings.FILE_STORAGE_DIR, use_case, hash12[:2], "images"
    )
    if not os.path.isdir(img_dir):
        return None
    for fname in os.listdir(img_dir):
        stem, ext = os.path.splitext(fname)
        if stem == image_id and ext.lower() in _IMAGE_EXTS:
            return os.path.join(img_dir, fname)
    return None

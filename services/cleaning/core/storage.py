"""Persistent file storage for original uploaded documents."""

from __future__ import annotations

import hashlib
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

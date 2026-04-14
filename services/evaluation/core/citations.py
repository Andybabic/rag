"""Citation mapping – maps [1], [2], etc. in an answer to source chunks."""

from __future__ import annotations

import re

_REF_PATTERN = re.compile(r"\[(\d+)]")


def map_citations(answer: str, chunks: list[dict]) -> list[dict]:
    """Extract citation references from the answer and map to chunks.

    References are 1-indexed: [1] maps to chunks[0], [2] to chunks[1], etc.
    """
    refs = _REF_PATTERN.findall(answer)
    seen: set[int] = set()
    citations: list[dict] = []

    for ref_str in refs:
        ref_num = int(ref_str)
        if ref_num in seen:
            continue
        seen.add(ref_num)

        idx = ref_num - 1
        if idx < 0 or idx >= len(chunks):
            continue

        chunk = chunks[idx]
        meta = chunk.get("metadata", {})
        text = chunk.get("text", "")
        excerpt = text[:200] if len(text) > 200 else text

        citations.append({
            "ref": f"[{ref_num}]",
            "file_name": meta.get("file_name", ""),
            "page": meta.get("page"),
            "excerpt": excerpt,
            "score": chunk.get("score", 0.0),
            "stored_path": meta.get("stored_path", ""),
        })

    return citations

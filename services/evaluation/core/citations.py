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


# Cleanups applied after removing an unresolved [n]: collapse the gap so the
# sentence stays readable ("gemäß  sofort" → "gemäß sofort", "(Quelle )" → "").
_EMPTY_PAREN_RE = re.compile(r"\(\s*(?:Quelle|Quellen|siehe|vgl\.?)?\s*\)")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")


def strip_unresolved_refs(answer: str, citations: list[dict]) -> str:
    """Remove ``[n]`` markers that map to no source.

    A small synthesizer model sometimes cites a pool index that does not
    exist (e.g. ``[14]`` when only 9 chunks were retrieved). Such a number
    renders as a clickable badge that leads nowhere. Dropping it keeps the
    rule "every citation number in the answer opens a real document".
    """
    valid = {
        int(c["ref"].strip("[]"))
        for c in citations
        if c.get("ref", "").strip("[]").isdigit()
    }
    cleaned = _REF_PATTERN.sub(
        lambda m: m.group(0) if int(m.group(1)) in valid else "", answer
    )
    if cleaned == answer:
        return answer
    cleaned = _EMPTY_PAREN_RE.sub("", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    return cleaned.strip()

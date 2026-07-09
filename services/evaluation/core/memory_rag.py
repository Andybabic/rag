"""Memory RAG — a dedicated per-use-case store of past Q&A.

Each answered user query becomes ONE chunk (``Frage: … / Antwort: …``) in a
separate ``mem_<use_case>`` Qdrant collection. On a new (typically vague) query
the most similar past Q&A are recalled and fed to the manager **as information
only** — never as a citation source.

Isolation: the agent's document SEARCH is scoped to the use case's document
collection prefixes (``<use_case>_…``). ``mem_<use_case>`` does not match those,
so recalled memory informs planning/answering but can never surface as a cited
document. This module talks to the embedding + vectordb services directly and
is fully best-effort: any failure degrades to "no memory", never raising.
"""

from __future__ import annotations

import hashlib
import logging

import httpx
from config import settings

logger = logging.getLogger(__name__)


def _collection(use_case: str) -> str:
    # Prefix ``mem_`` deliberately does NOT match the document prefix
    # ``<use_case>_``, keeping memory out of the agent's document search.
    return f"mem_{use_case}"


async def _embed(text: str, use_case: str) -> tuple[list[float] | None, str | None]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.EMBEDDING_SERVICE_URL}/v1/embed",
            json={"type": "text", "content": text, "use_case": use_case},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("vector"), data.get("model")


async def remember(use_case: str, question: str, answer: str) -> None:
    """Store one Q&A pair as a memory chunk. Best-effort; never raises.

    The chunk id is derived from the question so re-asking the same thing
    updates the existing memory instead of piling up duplicates.
    """
    q = (question or "").strip()
    a = (answer or "").strip()
    if not q or not a:
        return
    text = f"Frage: {q}\nAntwort: {a}"
    try:
        vector, model = await _embed(text, use_case)
        if not vector:
            return
        cid = "mem_" + hashlib.sha1(f"{use_case}|{q}".encode("utf-8")).hexdigest()[:16]
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.VECTORDB_SERVICE_URL}/v1/upsert",
                json={
                    "collection": _collection(use_case),
                    "embeddings": [{
                        "chunk_id": cid,
                        "vector": vector,
                        "metadata": {
                            "chunk_id": cid,
                            "text": text,
                            "question": q,
                            "answer": a[:2000],
                            "kind": "memory",
                        },
                    }],
                    "embed_model": model,
                },
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — memory is best-effort
        logger.warning("Memory remember failed for use case %r: %s", use_case, exc)


async def recall(
    use_case: str, query: str, *, top_k: int = 3, min_score: float = 0.3
) -> list[dict]:
    """Retrieve up to ``top_k`` relevant past Q&A (score-filtered).

    Returns ``[]`` on any failure or when the memory collection doesn't exist
    yet (first ever query for the use case).
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        vector, model = await _embed(q, use_case)
        if not vector:
            return []
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.VECTORDB_SERVICE_URL}/v1/search",
                json={
                    "collection": _collection(use_case),
                    "vector": vector,
                    "top_k": top_k,
                    "embed_model": model,
                },
            )
        # No memory yet (collection absent) or model mismatch → just no memory.
        if resp.status_code in (404, 409):
            return []
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:  # noqa: BLE001 — memory is best-effort
        logger.warning("Memory recall failed for use case %r: %s", use_case, exc)
        return []

    out: list[dict] = []
    for res in results:
        if (res.get("score") or 0) < min_score:
            continue
        md = res.get("metadata") or {}
        out.append({
            "question": md.get("question", ""),
            "answer": md.get("answer", ""),
            "score": res.get("score", 0),
        })
    return out


def format_memory(mems: list[dict], *, answer_chars: int = 400) -> str:
    """Render recalled Q&A as compact planner context (information, not source)."""
    if not mems:
        return ""
    lines: list[str] = []
    for m in mems:
        ans = (m.get("answer") or "").strip()[:answer_chars]
        lines.append(f"- Frühere Frage: {m.get('question', '')}\n  Damalige Antwort: {ans}")
    return "\n".join(lines)

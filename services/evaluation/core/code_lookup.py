"""Look up the codes a question names, verbatim — Ctrl+F over the use case's chunks.

Embeddings blur a code like ``A30042`` or ``p0251`` into whatever else the
question says, so the one chunk that explains it never ranks. Such codes are
therefore searched as plain text first. Where they occur decides what happens:

* a few places: the chunks go to the specialists as candidates, ranked first,
* many places: the model narrows them down with the question,
* still no clear pick: the user is asked, with a snippet per place.

A code that occurs nowhere is reported as such; the agent then asks instead of
answering from neighbouring topics (see ``action_search``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx
from config import settings
from core.llm import call_llm
from core.use_cases import get_use_case_prefixes

logger = logging.getLogger(__name__)

# A/F/N + digits: warnings, faults, messages. p/r + digits: drive parameters.
_TERM_RE = re.compile(r"\b(?:[AFN]\d{4,5}|[pr]\d{4,5})\b")

# Up to this many places are passed on as they are.
_MAX_AUTO = 3
# Places shown to the model / the user when there are more.
_MAX_LISTED = 8
# A term this widespread (r0949, p0010) says nothing about where to look: the
# plain text search is left out and the question is answered as before.
_TOO_COMMON = 15
_SNIPPET_RADIUS = 110

# Chunks the specialists must see. Set by the manager before the fan-out; the
# sub-agents run as tasks created afterwards, so they inherit the value.
_PINNED: ContextVar[list[dict] | None] = ContextVar("pinned_chunks", default=None)


def pinned_chunks() -> list[dict]:
    return _PINNED.get() or []


def pin(chunks: list[dict]) -> None:
    _PINNED.set(chunks)


def extract_terms(*texts: str) -> list[str]:
    """Codes and parameter names in the texts, in order of appearance."""
    terms: list[str] = []
    for text in texts:
        for term in _TERM_RE.findall(text or ""):
            if term not in terms:
                terms.append(term)
    return terms


@dataclass
class Place:
    """Everything one page of one document says about the looked-up terms."""

    file_name: str
    page: int | None
    terms: list[str] = field(default_factory=list)
    snippet: str = ""
    chunks: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        name = self.file_name.rsplit("/", 1)[-1]
        return f"{name} S. {self.page}" if self.page is not None else name


@dataclass
class Lookup:
    terms: list[str]
    places: list[Place]
    pinned: list[dict] = field(default_factory=list)
    ask: str = ""
    narrowed: bool = False
    too_common: bool = False
    ms: int = 0

    def describe(self) -> str:
        """One readable block for the step log."""
        names = ", ".join(self.terms)
        if not self.places:
            return f"{names}: kein Treffer in den Dokumenten."
        documents = len({p.file_name for p in self.places})
        if self.too_common:
            return (f"{names}: {len(self.places)} Stellen in {documents} Dokument(en) — "
                    "zu häufig, um die Suche einzugrenzen.")
        lines = [f"{names}: {len(self.places)} Stelle(n) in {documents} Dokument(en)."]
        lines += [f"- {p.label} ({len(p.chunks)}×): {p.snippet}" for p in self.places[:_MAX_LISTED]]
        return "\n".join(lines)

    def summary(self) -> list[dict]:
        """Per place: where it is, how many chunks, which terms, a snippet."""
        return [
            {"file_name": p.file_name, "page": p.page, "chunks": len(p.chunks),
             "terms": p.terms, "snippet": p.snippet}
            for p in self.places
        ]


def _snippet(text: str, terms: list[str]) -> str:
    flat = " ".join((text or "").split())
    for term in terms:
        at = flat.find(term)
        if at >= 0:
            start = max(0, at - _SNIPPET_RADIUS)
            end = min(len(flat), at + len(term) + _SNIPPET_RADIUS)
            return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")
    return flat[: 2 * _SNIPPET_RADIUS]


async def _use_case_collections(use_case: str) -> list[str]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{settings.VECTORDB_SERVICE_URL}/v1/collections")
        resp.raise_for_status()
    prefixes = get_use_case_prefixes(use_case)
    return [
        c["name"] for c in resp.json()["collections"]
        if any(c["name"].startswith(p) for p in prefixes)
    ]


async def find_places(use_case: str, terms: list[str]) -> list[Place]:
    """Places in the use case's documents that contain any of the terms."""
    collections = await _use_case_collections(use_case)
    if not collections or not terms:
        return []
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.VECTORDB_SERVICE_URL}/v1/points/find",
            json={"collections": collections, "terms": terms},
        )
        resp.raise_for_status()

    places: dict[tuple, Place] = {}
    seen: dict[str, dict] = {}
    for term, chunks in resp.json()["hits"].items():
        for chunk in chunks:
            if chunk["chunk_id"] in seen:
                continue
            meta = {**chunk["metadata"], "_collection": chunk["collection"]}
            result = {"chunk_id": chunk["chunk_id"], "score": 1.0, "text": chunk["text"],
                      "metadata": meta, "_query_index": 0}
            seen[chunk["chunk_id"]] = result
            key = (meta.get("file_name", ""), meta.get("page"))
            place = places.setdefault(key, Place(file_name=key[0], page=key[1]))
            place.chunks.append(result)
    for place in places.values():
        place.terms = [t for t in terms if any(t in c["text"] for c in place.chunks)]
        place.snippet = _snippet(place.chunks[0]["text"], place.terms or terms)
    # Most chunks first: where the term is discussed, not merely mentioned.
    return sorted(places.values(), key=lambda p: (-len(p.chunks), p.file_name, p.page or 0))


def ask_text(terms: list[str], places: list[Place]) -> str:
    """The question to the user when the places cannot be told apart."""
    documents = len({p.file_name for p in places})
    lines = [
        f"{', '.join(terms)} kommt an {len(places)} Stellen in {documents} "
        f"Dokument{'en' if documents != 1 else ''} vor. Welche ist gemeint?"
    ]
    for i, place in enumerate(places[:_MAX_LISTED], 1):
        lines.append(f"{i}. {place.label} ({len(place.chunks)}×): „{place.snippet}“")
    if len(places) > _MAX_LISTED:
        lines.append(f"… und {len(places) - _MAX_LISTED} weitere Stellen.")
    lines.append("Bitte nennen Sie ein Stichwort aus der passenden Stelle, z. B. die Komponente.")
    return "\n".join(lines)


async def _narrow(question: str, places: list[Place], use_case: str) -> list[Place] | None:
    """The places the question is about, or ``None`` if it does not say."""
    listed = places[:_MAX_LISTED]
    numbered = "\n".join(f"{i}. {p.label}: {p.snippet}" for i, p in enumerate(listed, 1))
    messages = [
        {"role": "system", "content": (
            "Du wählst aus nummerierten Textstellen eines Handbuchs die aus, "
            "um die es in der Frage des Nutzers geht. Antworte NUR mit JSON: "
            '{"stellen": [Nummern]}. Wähle höchstens '
            f"{_MAX_AUTO}. Lässt die Frage nicht eindeutig erkennen, welche "
            'Stellen gemeint sind, antworte {"stellen": []}.'
        )},
        {"role": "user", "content": f"Frage: {question}\n\nTextstellen:\n{numbered}"},
    ]
    try:
        raw = await call_llm(messages, use_case=use_case, max_tokens=120)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Code lookup: narrowing failed: %s", exc)
        return None
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        numbers = json.loads(match.group(0)).get("stellen", [])
    except (ValueError, AttributeError):
        return None
    picked = [
        listed[n - 1] for n in dict.fromkeys(numbers)
        if isinstance(n, int) and 1 <= n <= len(listed)
    ]
    return picked if 1 <= len(picked) <= _MAX_AUTO else None


async def lookup(question: str, use_case: str, context: list[str] | None = None) -> Lookup | None:
    """Look up the codes in ``question`` (and earlier user messages in ``context``).

    ``None`` when nothing in the question looks like a code, or the lookup
    failed — the pipeline then runs as before. Never raises.
    """
    terms = extract_terms(question, *(context or []))
    if not terms:
        return None
    started = time.perf_counter()
    try:
        places = await find_places(use_case, terms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Code lookup failed for %s: %s", terms, exc)
        return None

    result = Lookup(terms=terms, places=places)
    if len(places) > _TOO_COMMON:
        result.too_common = True
        chosen: list[Place] | None = []
    elif len(places) <= _MAX_AUTO:
        chosen = places
    else:
        # The follow-up ("Lüfter") only makes sense together with the question
        # it answers, so the model sees both.
        chosen = await _narrow(" ".join([*(context or []), question]), places, use_case)
        result.narrowed = chosen is not None
        if chosen is None:
            result.ask = ask_text(terms, places)
    result.pinned = [c for p in chosen or [] for c in p.chunks]
    result.ms = round((time.perf_counter() - started) * 1000)
    return result

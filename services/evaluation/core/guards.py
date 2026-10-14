"""Deterministic guards on text an LLM writes for the pipeline itself.

Search queries, sub-queries and gap questions are written by the model, and
the model "helpfully" adds what it knows: it dated "der verfluchte Dienstag"
(the 2024 pager attacks) to the Beirut port explosion of 2020, and searched
for that. Retrieval then hunts the wrong event and the specialist rejects the
right evidence because it does not match the invented year. Prompt rules did
not stop this; these guards do, for every role that writes such text.
"""

from __future__ import annotations

import re

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_CONNECTOR = r"(?:\s*(?:/|–|-|,|oder|und|bis|or|and|to)\s*)"
_YEAR_RUN_RE = re.compile(
    rf"{_CONNECTOR}?\b(?:19|20)\d{{2}}\b(?:{_CONNECTOR}\b(?:19|20)\d{{2}}\b)*"
)


def strip_invented_years(text: str, allowed_text: str) -> str:
    """``text`` without years that appear nowhere in ``allowed_text``.

    ``allowed_text`` is everything the writer legitimately had: the user's
    question, the conversation, the domain prompt and the documents seen so
    far. A year from any of those stays; a parenthetical carrying an invented
    year is invented as a whole ("(Beirut 2020)") and goes entirely; a bare
    year takes a dangling connector with it ("London 2023 oder 2024" → "London").
    """
    allowed = set(_YEAR_RE.findall(allowed_text or ""))

    def invented(fragment: str) -> bool:
        return any(y not in allowed for y in _YEAR_RE.findall(fragment))

    if not invented(text or ""):
        return text
    cleaned = re.sub(
        r"\s*\(([^()]*)\)", lambda m: "" if invented(m.group(1)) else m.group(0), text
    )
    cleaned = _YEAR_RUN_RE.sub(lambda m: "" if invented(m.group(0)) else m.group(0), cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()

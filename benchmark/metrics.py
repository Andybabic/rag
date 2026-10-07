"""Deterministic answer and retrieval metrics – no LLM, fully reproducible."""

from __future__ import annotations

import re
from collections import Counter

_CITE_RE = re.compile(r"\[\d+(?:\s*[,;]\s*\d+)*\]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
# Digit groups incl. separators: "14.000", "14,000", "2,5", "2024".
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*$")

_STOP_DE = {
    "der",
    "die",
    "das",
    "und",
    "ist",
    "nicht",
    "mit",
    "von",
    "den",
    "ein",
    "eine",
    "einer",
    "eines",
    "einem",
    "einen",
    "im",
    "zu",
    "zum",
    "zur",
    "auf",
    "für",
    "wurde",
    "wurden",
    "sich",
    "des",
    "dem",
    "als",
    "auch",
    "es",
    "sie",
    "er",
    "wird",
    "werden",
    "bei",
    "aus",
    "nach",
    "wie",
    "oder",
    "aber",
    "noch",
    "nur",
    "hat",
    "haben",
    "sind",
    "war",
    "waren",
    "dass",
    "über",
    "vor",
    "am",
    "um",
    "so",
    "kann",
    "diese",
    "dieser",
}
_STOP_EN = {
    "the",
    "and",
    "is",
    "of",
    "to",
    "was",
    "with",
    "for",
    "that",
    "on",
    "are",
    "by",
    "which",
    "has",
    "have",
    "had",
    "were",
    "be",
    "been",
    "it",
    "its",
    "this",
    "these",
    "from",
    "at",
    "as",
    "or",
    "but",
    "not",
    "an",
    "a",
    "will",
    "would",
    "their",
    "they",
    "than",
    "also",
    "while",
    "after",
    "before",
    "more",
    "most",
    "can",
}
# "in" and "an" exist in both languages; useless for detection.
_DETECT_DE = _STOP_DE - {"in", "an", "so"}
_DETECT_EN = _STOP_EN - {"in", "an", "a", "so"}
_STOP = _STOP_DE | _STOP_EN | {"in"}

_REFUSAL_PATTERNS = [
    r"nicht zuverlässig beantworten",
    r"keine (?:passenden |relevanten |ausreichenden )?(?:informationen|angaben|hinweise)",
    r"nicht in den (?:vorliegenden |gefundenen )?(?:dokumenten|artikeln|quellen)",
    r"lässt sich (?:aus den .{0,40})?nicht beantworten",
    r"cannot (?:reliably )?answer",
    r"(?:no|not enough) (?:relevant |sufficient )?information",
    r"not (?:found|contained|mentioned) in the (?:documents|articles|sources)",
]
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.IGNORECASE)


def strip_citations(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _CITE_RE.sub("", text or "")).strip()


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def content_tokens(text: str) -> list[str]:
    return [w for w in words(text) if w not in _STOP]


def token_overlap(answer: str, gold: str) -> dict:
    """Bag-of-words overlap without stopwords.

    precision – share of the answer that also appears in the gold answer;
                1 - precision is the "extra text" the answer carries
    recall    – share of the gold answer covered by the answer
    """
    a = Counter(content_tokens(strip_citations(answer)))
    g = Counter(content_tokens(gold))
    common = sum((a & g).values())
    if not a or not g:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    precision = common / sum(a.values())
    recall = common / sum(g.values())
    f1 = 2 * precision * recall / (precision + recall) if common else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def normalize_number(raw: str) -> str:
    """Map DE and EN spellings of one number to one form.

    "14.000" / "14,000" → "14000", "2,5" / "2.5" → "2.5".
    """
    if _THOUSANDS_RE.match(raw):
        return re.sub(r"[.,]", "", raw)
    return raw.replace(",", ".")


def numbers(text: str) -> set[str]:
    return {normalize_number(n) for n in _NUM_RE.findall(strip_citations(text))}


def number_recall(answer: str, gold: str) -> float | None:
    """Share of the gold answer's numbers (dates, counts, amounts) in the answer.

    ``None`` when the gold answer has no numbers – such rows don't count.
    """
    expected = numbers(gold)
    if not expected:
        return None
    return len(expected & numbers(answer)) / len(expected)


def length_ratio(answer: str, gold: str) -> float:
    """Answer length in words relative to the gold answer (1.0 = same length)."""
    return len(words(strip_citations(answer))) / max(1, len(words(gold)))


def detect_language(text: str) -> str:
    """Rough de/en guess from function words – enough for "right language?"."""
    tokens = words(strip_citations(text))
    de = sum(t in _DETECT_DE for t in tokens)
    en = sum(t in _DETECT_EN for t in tokens)
    if de == en:
        return "unknown"
    return "de" if de > en else "en"


def looks_like_refusal(answer: str, compliance_verdict: str = "") -> bool:
    if compliance_verdict == "REFUSE":
        return True
    return bool(_REFUSAL_RE.search(answer or ""))


def unique(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(s for s in seq if s))


def retrieval_metrics(
    retrieved_files: list[str], support: dict[str, int], ks: tuple[int, ...] = (1, 3, 5, 10)
) -> dict:
    """Did the agent's chunk pool contain the supporting articles?

    ``retrieved_files`` is the file of each global chunk in pool order (one
    article can contribute several chunks). Supporting = is_support > 0.
    """
    relevant = {f for f, level in support.items() if level > 0}
    docs = unique(retrieved_files)
    out: dict = {"retrieved_docs": len(docs)}
    if not relevant:
        return out
    for k in ks:
        out[f"recall@{k}"] = len(relevant & set(docs[:k])) / len(relevant)
    out["recall_all"] = len(relevant & set(docs)) / len(relevant)
    first = next((i for i, d in enumerate(docs, 1) if d in relevant), None)
    out["mrr"] = 1.0 / first if first else 0.0
    sufficient = {f for f, level in support.items() if level == 2}
    if sufficient:
        out["sufficient_found"] = bool(sufficient & set(docs))
    return out


def citation_metrics(cited_files: list[str], support: dict[str, int]) -> dict:
    """Are the cited sources the supporting articles?

    distractor_share – cited documents that are not supporting for this
    question (its own distractors or articles of other questions).
    """
    relevant = {f for f, level in support.items() if level > 0}
    cited = set(unique(cited_files))
    out: dict = {"cited_docs": len(cited)}
    if not cited:
        out.update(
            {
                "citation_precision": None,
                "citation_recall": 0.0 if relevant else None,
                "distractor_share": None,
            }
        )
        return out
    hits = cited & relevant
    out["citation_precision"] = len(hits) / len(cited)
    out["citation_recall"] = len(hits) / len(relevant) if relevant else None
    out["distractor_share"] = len(cited - relevant) / len(cited)
    return out


def score_answer(item: dict, result: dict) -> dict:
    """All deterministic metrics for one question/answer pair."""
    answer = result.get("answer", "") or ""
    gold = item["gold"]
    verdict = result.get("compliance_verdict", "")
    scores: dict = {
        "refused_heuristic": looks_like_refusal(answer, verdict),
        "answer_lang": detect_language(answer),
        "answer_words": len(words(strip_citations(answer))),
    }
    scores["lang_match"] = scores["answer_lang"] == item["lang"]
    if item.get("expect_answer", True):
        scores.update(token_overlap(answer, gold))
        scores["number_recall"] = number_recall(answer, gold)
        scores["length_ratio"] = length_ratio(answer, gold)
        scores.update(retrieval_metrics(result.get("chunk_files", []), item["support"]))
        scores.update(citation_metrics(result.get("cited_files", []), item["support"]))
    return scores

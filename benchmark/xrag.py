"""XRAG dataset: download, sampling and corpus construction.

Dataset: https://huggingface.co/datasets/AmazonScience/XRAG (CC BY-NC 4.0).
Every row carries 8 articles (supporting + distracting, see ``is_support``).
The corpus pools the articles of ALL sampled questions, so retrieval has to
find the right ones among hundreds of documents instead of 8.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from urllib.parse import urlparse

import httpx
from settings import CACHE_DIR

DATASET_URL = "https://huggingface.co/datasets/AmazonScience/XRAG/resolve/main/xrag.{split}.jsonl"
LANGUAGE = "de"


def cached_rows_path(split: str) -> Path:
    return CACHE_DIR / f"xrag.{split}.{LANGUAGE}.jsonl"


def download(split: str) -> Path:
    """Download the split once and keep only the German rows (~1/4 of it).

    The English version of every German question is part of the same row
    (``en_question`` / ``en_answer``), so nothing else is needed.
    """
    target = cached_rows_path(split)
    if target.is_file():
        return target
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    url = DATASET_URL.format(split=split)
    print(f"Lade {url} …")
    kept = read = 0
    with (
        httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as resp,
        tmp.open("w", encoding="utf-8") as out,
    ):
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.strip():
                continue
            read += 1
            row = json.loads(line)
            if row.get("language") == LANGUAGE:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                kept += 1
            if read % 500 == 0:
                print(f"  {read} Zeilen gelesen, {kept} deutsch")
    tmp.rename(target)
    print(f"  fertig: {kept} deutsche Fragen → {target}")
    return target


def load_rows(split: str) -> list[dict]:
    path = download(split)
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def article_file_name(url: str) -> str:
    """Stable per URL, so re-runs recognise already ingested articles."""
    return f"xrag_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}.md"


def is_mono(row: dict) -> bool:
    return str(row.get("task", "")).startswith("mono")


def sample(rows: list[dict], *, mono: int, multi: int, negative: int, seed: int) -> dict:
    """Pick question rows per group, reproducibly.

    Negative rows are questions whose articles stay OUT of the index; any row
    sharing an article URL with the corpus is skipped, otherwise the "missing"
    answer would be findable after all.
    """
    rng = random.Random(seed)
    mono_rows = sorted((r for r in rows if is_mono(r)), key=lambda r: r["id"])
    multi_rows = sorted((r for r in rows if not is_mono(r)), key=lambda r: r["id"])
    rng.shuffle(mono_rows)
    rng.shuffle(multi_rows)

    picked_mono = mono_rows[:mono]
    picked_multi = multi_rows[:multi]
    corpus_urls = {u for r in picked_mono + picked_multi for u in r["urls"]}

    picked_neg: list[dict] = []
    for row in mono_rows[mono:]:
        if len(picked_neg) >= negative:
            break
        if not corpus_urls.intersection(row["urls"]):
            picked_neg.append(row)
    return {"mono": picked_mono, "multi": picked_multi, "neg": picked_neg}


def _article_markdown(url: str, date: str, text: str) -> str:
    host = urlparse(url).netloc.removeprefix("www.")
    day = (date or "")[:10]
    header = f"# {host} – {day}" if day else f"# {host}"
    return f"{header}\n\nQuelle: {url}\n\n{text.strip()}\n"


def build_corpus(
    groups: dict, corpus_dir: Path, *, distractors: int | None = None
) -> dict[str, dict]:
    """Write the articles of the mono/multi rows as Markdown files.

    Per question all supporting articles go in, plus its first ``distractors``
    distracting ones (``None`` = all, usually 6). Fewer distractors mean less
    to upload and easier retrieval – the other questions' articles still compete.

    Returns the manifest ``file_name → {url, date}``. Articles shared between
    questions are written once.
    """
    corpus_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for row in groups["mono"] + groups["multi"]:
        kept_distractors = 0
        # article_order is the presentation order XRAG asks for; applying it
        # keeps the selection and upload order reproducible.
        for idx in row.get("article_order") or range(len(row["articles"])):
            if int(row["is_support"][idx]) == 0:
                if distractors is not None and kept_distractors >= distractors:
                    continue
                kept_distractors += 1
            url = row["urls"][idx]
            name = article_file_name(url)
            if name in manifest:
                continue
            date = str(row["dates"][idx]) if row.get("dates") else ""
            (corpus_dir / name).write_text(
                _article_markdown(url, date, row["articles"][idx]), encoding="utf-8"
            )
            manifest[name] = {"url": url, "date": date}
    return manifest


def support_map(row: dict) -> dict[str, int]:
    """file_name → is_support (0 none, 1 partial, 2 sufficient) for one row."""
    return {
        article_file_name(url): int(level)
        for url, level in zip(row["urls"], row["is_support"], strict=False)
    }


def build_queries(groups: dict, variants: list[str]) -> list[dict]:
    """One entry per (variant, question).

    de    – German question, English articles (XRAG monolingual retrieval)
    en    – the same rows asked in English: the baseline for "de"
    multi – German question, German + English articles
    neg   – German question whose articles are not indexed → refusal expected
    """
    queries: list[dict] = []

    def add(variant: str, row: dict, lang: str, *, expect_answer: bool = True) -> None:
        en = lang == "en"
        queries.append(
            {
                "key": f"{variant}:{row['id']}",
                "variant": variant,
                "id": row["id"],
                "lang": lang,
                "query": row["en_question"] if en else row["question"],
                "gold": row["en_answer"] if en else row["answer"],
                "q_type": row.get("q_type", ""),
                "crossdoc_type": row.get("crossdoc_type"),
                "expect_answer": expect_answer,
                "support": support_map(row) if expect_answer else {},
            }
        )

    for row in groups["mono"]:
        if "de" in variants:
            add("de", row, "de")
        if "en" in variants:
            add("en", row, "en")
    if "multi" in variants:
        for row in groups["multi"]:
            add("multi", row, "de")
    if "neg" in variants:
        for row in groups["neg"]:
            add("neg", row, "de", expect_answer=False)
    return queries

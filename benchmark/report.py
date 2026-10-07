"""Aggregate scored results into summary.json + summary.md."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

VARIANT_LABEL = {
    "de": "DE-Frage, EN-Artikel",
    "en": "EN-Frage, EN-Artikel",
    "multi": "DE-Frage, DE+EN-Artikel",
    "neg": "Negativ (Antwort nicht im Index)",
}

# (key, label, format) of the headline table – also what the run comparison diffs.
HEADLINE = [
    ("judge_score", "Judge-Score", "pct"),
    ("correct_rate", "richtig", "pct"),
    ("fact_recall", "Fakten-Abdeckung", "pct"),
    ("number_recall", "Zahlen-Treffer", "pct"),
    ("f1", "Token-F1", "pct"),
    ("precision", "Token-Precision", "pct"),
    ("length_ratio_median", "Länge vs. Gold (Median)", "x"),
    ("extra_claims", "Zusatzaussagen Ø", "num"),
    ("recall@5", "Retrieval Recall@5", "pct"),
    ("recall_all", "Retrieval Recall (Pool)", "pct"),
    ("citation_precision", "Zitat-Precision", "pct"),
    ("citation_recall", "Zitat-Recall", "pct"),
    ("distractor_share", "Zitate aus Ablenkern", "pct"),
    ("lang_match", "Antwortsprache korrekt", "pct"),
    ("false_refusal", "Fälschlich abgelehnt", "pct"),
    ("latency_p50_s", "Latenz p50", "s"),
    ("latency_p95_s", "Latenz p95", "s"),
]
NEG_HEADLINE = [
    ("refusal_rate", "Korrekt abgelehnt", "pct"),
    ("hallucination_rate", "Trotzdem geantwortet", "pct"),
    ("latency_p50_s", "Latenz p50", "s"),
]


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


def _median(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _pctl(values, q: float) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def _rate(flags) -> float | None:
    vals = [bool(f) for f in flags if f is not None]
    return sum(vals) / len(vals) if vals else None


def refused(row: dict) -> bool:
    if row.get("judge_refused") is not None:
        return bool(row["judge_refused"])
    return bool(row.get("refused_heuristic"))


def quality(row: dict) -> float | None:
    """Main per-question score: judge if available, else token recall."""
    if row.get("judge_score") is not None:
        return row["judge_score"]
    return row.get("recall")


def aggregate(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    latency = [r["processing_ms"] / 1000 for r in ok if r.get("processing_ms") is not None]
    out: dict = {
        "n": len(rows),
        "errors": len(rows) - len(ok),
        "latency_p50_s": _median(latency),
        "latency_p95_s": _pctl(latency, 0.95),
        "agent_steps": _mean(r.get("agent_steps") for r in ok),
        "memory_used": sum(bool(r.get("memory_used")) for r in ok),
        "compliance": dict(Counter(r.get("compliance_verdict") or "-" for r in ok)),
    }
    if ok and not ok[0].get("expect_answer", True):
        out["refusal_rate"] = _rate(refused(r) for r in ok)
        out["hallucination_rate"] = _rate(
            r.get("hallucinated") if r.get("hallucinated") is not None else not refused(r)
            for r in ok
        )
        return out

    verdicts = Counter(r.get("judge_verdict") for r in ok if r.get("judge_verdict"))
    judged = sum(verdicts.values())
    out.update(
        {
            "judge_score": _mean(r.get("judge_score") for r in ok),
            "correct_rate": verdicts["correct"] / judged if judged else None,
            "partial_rate": verdicts["partial"] / judged if judged else None,
            "incorrect_rate": verdicts["incorrect"] / judged if judged else None,
            "length_ratio_median": _median(r.get("length_ratio") for r in ok),
            "answer_words_median": _median(r.get("answer_words") for r in ok),
            "false_refusal": _rate(refused(r) for r in ok),
            "sufficient_found": _rate(r.get("sufficient_found") for r in ok),
        }
    )
    for key in (
        "fact_recall",
        "number_recall",
        "f1",
        "precision",
        "recall",
        "extra_claims",
        "contradictions",
        "recall@1",
        "recall@3",
        "recall@5",
        "recall@10",
        "recall_all",
        "mrr",
        "citation_precision",
        "citation_recall",
        "distractor_share",
        "lang_match",
    ):
        out[key] = _mean(r.get(key) for r in ok)
    return out


def breakdown(rows: list[dict], field: str) -> dict:
    groups: dict = defaultdict(list)
    for r in rows:
        if not r.get("error"):
            groups[str(r.get(field))].append(r)
    return {
        k: {
            "n": len(v),
            "quality": _mean(quality(r) for r in v),
            "recall_all": _mean(r.get("recall_all") for r in v),
        }
        for k, v in sorted(groups.items())
    }


def paired_gap(rows: list[dict], a: str, b: str) -> dict | None:
    """Quality(a) − quality(b) over questions asked in both variants."""
    by = {
        v: {r["id"]: quality(r) for r in rows if r["variant"] == v and not r.get("error")}
        for v in (a, b)
    }
    ids = [i for i in by[a] if i in by[b] and by[a][i] is not None and by[b][i] is not None]
    if not ids:
        return None
    diffs = [by[a][i] - by[b][i] for i in ids]
    return {
        "n": len(ids),
        "delta": statistics.fmean(diffs),
        f"{a}_better": sum(d > 0 for d in diffs),
        f"{b}_better": sum(d < 0 for d in diffs),
    }


def summarize(rows: list[dict], config: dict) -> dict:
    variants = [v for v in ("de", "en", "multi", "neg") if any(r["variant"] == v for r in rows)]
    summary = {
        "config": config,
        "variants": {v: aggregate([r for r in rows if r["variant"] == v]) for v in variants},
        "by_q_type": {
            v: breakdown([r for r in rows if r["variant"] == v], "q_type")
            for v in variants
            if v != "neg"
        },
        "by_crossdoc_type": {
            v: breakdown([r for r in rows if r["variant"] == v], "crossdoc_type")
            for v in variants
            if v != "neg"
        },
    }
    if gap := paired_gap(rows, "de", "en"):
        summary["de_vs_en"] = gap
    return summary


# ── markdown ────────────────────────────────────────────────


def _fmt(value, kind: str) -> str:
    if value is None:
        return "–"
    if kind == "pct":
        return f"{value * 100:.1f} %"
    if kind == "x":
        return f"{value:.2f}×"
    if kind == "s":
        return f"{value:.1f} s"
    return f"{value:.2f}"


def _delta(cur, prev, kind: str) -> str:
    if cur is None or prev is None:
        return ""
    d = cur - prev
    if abs(d) < 1e-9:
        return " (±0)"
    return (
        f" ({'+' if d > 0 else ''}{d * 100:.1f} pp)"
        if kind == "pct"
        else f" ({'+' if d > 0 else ''}{d:.2f})"
    )


def _table(headline, variants: dict, prev: dict | None) -> list[str]:
    names = list(variants)
    lines = ["| Kennzahl | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for key, label, kind in headline:
        cells = []
        for v in names:
            cur = variants[v].get(key)
            old = ((prev or {}).get("variants", {}).get(v) or {}).get(key)
            cells.append(_fmt(cur, kind) + _delta(cur, old, kind))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines


def _short(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " …"


def render_markdown(
    summary: dict, rows: list[dict], prev: dict | None, prev_name: str | None
) -> str:
    cfg = summary["config"]
    out = [
        f"# XRAG-Benchmark – {cfg.get('run')}",
        "",
        f"Use Case `{cfg.get('use_case')}` · Split `{cfg.get('split')}` · Seed {cfg.get('seed')} · "
        f"{cfg.get('documents')} Dokumente im Index "
        f"({cfg.get('distractors', 'alle')} Ablenker/Frage) · "
        f"Judge `{cfg.get('judge_model') or 'aus'}` · "
        f"Kermit-Modell `{cfg.get('kermit_model') or '?'}`",
    ]
    if prev_name:
        out.append(f"Werte in Klammern: Veränderung zu `{prev_name}`.")
    out += [
        "",
        "Varianten: " + " · ".join(f"**{v}** = {VARIANT_LABEL[v]}" for v in summary["variants"]),
        "",
    ]

    answered = {v: a for v, a in summary["variants"].items() if v != "neg"}
    if answered:
        out += ["## Ergebnis", ""] + _table(HEADLINE, answered, prev) + [""]
        for v, a in answered.items():
            out.append(
                f"- **{v}**: n={a['n']}, Fehler={a['errors']}, Compliance {a['compliance']}, "
                f"Ø {_fmt(a.get('agent_steps'), 'num')} Agenten-Schritte"
                + (f", Gedächtnis genutzt: {a['memory_used']}×" if a.get("memory_used") else "")
            )
        out.append("")
    if gap := summary.get("de_vs_en"):
        out += [
            "## Sprachlücke DE ↔ EN (gleiche Fragen)",
            "",
            f"Qualität DE − EN: **{gap['delta'] * 100:+.1f} pp** über {gap['n']} Fragen "
            f"(DE besser: {gap['de_better']}, EN besser: {gap['en_better']}).",
            "",
        ]
    if "neg" in summary["variants"]:
        out += (
            ["## Negativ-Kontrolle", ""]
            + _table(NEG_HEADLINE, {"neg": summary["variants"]["neg"]}, prev)
            + [""]
        )

    for title, key in (
        ("Nach Fragetyp", "by_q_type"),
        ("Nach Dokumentlage (crossdoc_type)", "by_crossdoc_type"),
    ):
        out += [f"## {title}", ""]
        if key == "by_crossdoc_type":
            out += [
                "0 = ein Artikel reicht · 1 = ein Artikel reicht, weitere ergänzen · "
                "2 = Antwort verteilt auf zwei Artikel",
                "",
            ]
        out += ["| Variante | Gruppe | n | Qualität | Retrieval Recall |", "|---|---|---|---|---|"]
        for v, groups in summary[key].items():
            for g, s in groups.items():
                out.append(
                    f"| {v} | {g} | {s['n']} | {_fmt(s['quality'], 'pct')} | "
                    f"{_fmt(s['recall_all'], 'pct')} |"
                )
        out.append("")

    errors = [r for r in rows if r.get("error")]
    if errors:
        out += (
            ["## Fehler", ""]
            + [f"- `{r['key']}`: {_short(r['error'], 200)}" for r in errors[:20]]
            + [""]
        )

    worst = sorted(
        (
            r
            for r in rows
            if r.get("expect_answer", True) and not r.get("error") and quality(r) is not None
        ),
        key=lambda r: (quality(r), r.get("recall_all") or 0),
    )[:10]
    if worst:
        out += ["## Schwächste Fälle", ""]
        for r in worst:
            out += [
                f"### `{r['key']}` · {r.get('q_type')} · Qualität {_fmt(quality(r), 'pct')} · "
                f"Retrieval {_fmt(r.get('recall_all'), 'pct')}",
                "",
                f"- **Frage:** {r['query']}",
                f"- **Gold:** {_short(r['gold'])}",
                f"- **Kermit:** {_short(r.get('answer', ''))}",
            ]
            if r.get("judge_reason"):
                out.append(f"- **Judge:** {r['judge_reason']}")
            out += [f"- request_id `{r.get('request_id')}` (Trace im Admin unter Abfragen)", ""]
    return "\n".join(out)


def write_report(run_dir: Path, rows: list[dict], config: dict) -> dict:
    summary = summarize(rows, config)
    prev, prev_name = None, None
    runs = sorted(
        p
        for p in run_dir.parent.iterdir()
        if p.is_dir() and p.name < run_dir.name and (p / "summary.json").is_file()
    )
    if runs:
        prev_name = runs[-1].name
        prev = json.loads((runs[-1] / "summary.json").read_text(encoding="utf-8"))
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(
        render_markdown(summary, rows, prev, prev_name), encoding="utf-8"
    )
    return summary

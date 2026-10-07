#!/usr/bin/env python3
"""Kermit XRAG benchmark.

    python benchmark/run.py all                 # alles: prepare → … → report
    python benchmark/run.py query --run <dir>   # einzelne Phase auf einem Lauf

Phasen: check, prepare, setup, ingest, query, score, report – und clean, das
alle Benchmark-Daten wieder aus Kermit entfernt (nicht Teil von all).
check testet Kermit und das Judge-LLM, bevor irgendetwas hochgeladen wird. Ein Lauf liegt in
benchmark/runs/<zeitstempel>/; ohne --run arbeiten die Phasen auf dem neuesten.
Abgebrochene Läufe lassen sich fortsetzen: ingest überspringt indexierte
Artikel, query bereits beantwortete Fragen, score bereits bewertete.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import xrag
from judge import Judge, JudgeConfigError, JudgeError
from kermit_client import Kermit, KermitError, compact_result
from metrics import score_answer, strip_citations
from report import VARIANT_LABEL, write_report
from settings import BENCH_DIR, RUNS_DIR, Settings, load_settings

PHASES = ["prepare", "setup", "ingest", "query", "score", "report"]
# Not part of "all": removes the benchmark's data from Kermit again.
CLEAN = "clean"
# Runs first in "all": connections to Kermit and the judge, before anything is
# uploaded or asked.
CHECK = "check"


# ── run directory helpers ───────────────────────────────────


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_run() -> Path:
    runs = sorted(p for p in RUNS_DIR.glob("*") if (p / "queries.jsonl").is_file())
    if not runs:
        sys.exit("Kein Lauf vorhanden – zuerst `python benchmark/run.py prepare` (oder `all`).")
    return runs[-1]


def load_config(run_dir: Path) -> dict:
    return json.loads((run_dir / "config.json").read_text(encoding="utf-8"))


def save_config(run_dir: Path, config: dict) -> None:
    (run_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8"
    )


class Progress:
    def __init__(self, total: int, label: str):
        self.total, self.label, self.done, self.t0 = total, label, 0, time.monotonic()

    def tick(self, note: str = "") -> None:
        self.done += 1
        elapsed = time.monotonic() - self.t0
        eta = elapsed / self.done * (self.total - self.done)
        print(
            f"  [{self.label}] {self.done}/{self.total}  {elapsed:5.0f}s  ETA {eta:5.0f}s  {note}",
            flush=True,
        )


# ── phases ──────────────────────────────────────────────────


async def phase_check(s: Settings) -> None:
    k = Kermit(s)
    try:
        await k.check()
    finally:
        await k.aclose()
    print(f"check: Kermit ok (Frontend {k.frontend})")
    if not s.judge_enabled:
        print("check: kein Judge konfiguriert – nur Kennzahlen ohne LLM")
        return
    try:
        judge = Judge(s)
    except JudgeConfigError as exc:
        raise KermitError(f"Judge: {exc}") from exc
    try:
        seconds = await judge.ping()
    except JudgeError as exc:
        raise KermitError(
            f"Judge {s.judge_provider}:{s.judge_model} unter {judge.url} nicht nutzbar – {exc}\n"
            "BENCH_JUDGE_* in benchmark/.env prüfen – oder BENCH_JUDGE_MODEL= leer setzen "
            "für Kennzahlen ohne Judge."
        ) from exc
    finally:
        await judge.aclose()
    print(f"check: Judge ok ({s.judge_provider}:{s.judge_model}, {seconds:.1f}s)")


def phase_prepare(s: Settings, run_dir: Path) -> None:
    rows = xrag.load_rows(s.split)
    groups = xrag.sample(rows, mono=s.mono, multi=s.multi, negative=s.negative, seed=s.seed)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = xrag.build_corpus(
        groups, run_dir / "corpus", distractors=None if s.distractors < 0 else s.distractors
    )
    queries = xrag.build_queries(groups, s.variants)
    write_jsonl(run_dir / "queries.jsonl", queries)
    (run_dir / "corpus.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    save_config(
        run_dir,
        {
            "run": run_dir.name,
            "split": s.split,
            "seed": s.seed,
            "mono": len(groups["mono"]),
            "multi": len(groups["multi"]),
            "negative": len(groups["neg"]),
            "distractors": "alle" if s.distractors < 0 else s.distractors,
            "variants": s.variants,
            "use_case": s.use_case,
            "documents": len(manifest),
            "judge_model": s.judge_model if s.judge_enabled else None,
        },
    )
    counts = {v: sum(q["variant"] == v for q in queries) for v in s.variants}
    print(f"prepare: {len(manifest)} Artikel, Abfragen {counts} → {run_dir}")


async def phase_setup(s: Settings, run_dir: Path, *, fresh: bool) -> None:
    k = Kermit(s)
    try:
        await k.check()
        prompt = (BENCH_DIR / "prompt.md").read_text(encoding="utf-8").strip()
        await k.upsert_use_case(prompt)
        print(f"setup: Use Case `{s.use_case}` angelegt/aktualisiert")
        if fresh:
            info = await k.wipe()
            print(
                f"setup: --fresh → {info['documents']} Dokumente, "
                f"Collections {info['collections']} gelöscht"
            )
    finally:
        await k.aclose()


async def phase_ingest(s: Settings, run_dir: Path) -> None:
    manifest = json.loads((run_dir / "corpus.json").read_text(encoding="utf-8"))
    k = Kermit(s)
    try:
        await k.check()
        indexed = {d["file_name"] for d in await k.list_documents()}
        foreign = indexed - set(manifest)
        if not indexed and await k.indexed_points():
            # Vectors the document list knows nothing about: an earlier upload
            # whose log entry failed. Unknown content – start clean.
            print(
                "ingest: Vektoren im Index ohne Eintrag in der Dokumentliste → Index wird geleert"
            )
            await k.wipe()
        elif foreign:
            # Leftovers from another sample would add distractors – and could
            # contain the answers to this run's negative questions.
            print(
                f"ingest: {len(foreign)} Dokumente gehören nicht zu diesem Korpus "
                "→ Index wird geleert"
            )
            await k.wipe()
            indexed = set()
        todo = [n for n in manifest if n not in indexed]
        print(f"ingest: {len(manifest) - len(todo)} bereits indexiert, {len(todo)} hochzuladen")
        if not todo:
            return
        await k.login()
        sem = asyncio.Semaphore(s.ingest_concurrency)
        progress = Progress(len(todo), "ingest")
        log_path = run_dir / "ingest.jsonl"
        failed: list[str] = []

        async def one(name: str) -> None:
            async with sem:
                t0 = time.monotonic()
                try:
                    res = await k.ingest(run_dir / "corpus" / name)
                    entry = {"file": name, "chunks": res.get("chunks"), "ok": True}
                except Exception as exc:  # noqa: BLE001 – one bad file must not stop the run
                    entry = {"file": name, "ok": False, "error": str(exc)}
                    failed.append(name)
                entry["ms"] = round((time.monotonic() - t0) * 1000)
                append_jsonl(log_path, entry)
                progress.tick(name + ("" if entry["ok"] else "  FEHLER"))

        # The first upload alone, then check that it reached the document
        # list: re-runs rely on it to skip indexed articles. If logging is
        # broken, stop now instead of uploading everything for nothing.
        await one(todo[0])
        if todo[0] not in failed and todo[0] not in {
            d["file_name"] for d in await k.list_documents()
        }:
            raise KermitError(
                "Upload ok, aber der Artikel fehlt in der Dokumentliste (ingestion_log). "
                "Ohne Eintrag würde jeder Lauf alles neu hochladen. Häufigste Ursache: "
                "ausstehende DB-Migration → `make migrate`, Services neu starten; "
                "Details in den evaluation-Logs („Failed to log ingestion“)."
            )
        await asyncio.gather(*(one(n) for n in todo[1:]))
        if failed:
            print(
                f"ingest: {len(failed)} fehlgeschlagen (Details in {log_path}); "
                "erneut `ingest` ausführen versucht sie nochmal"
            )
    finally:
        await k.aclose()


async def phase_query(s: Settings, run_dir: Path) -> None:
    queries = read_jsonl(run_dir / "queries.jsonl")
    results_path = run_dir / "results.jsonl"
    done = {r["key"] for r in read_jsonl(results_path) if not r.get("error")}
    k = Kermit(s)
    try:
        await k.check()
        sem = asyncio.Semaphore(s.query_concurrency)
        model: str | None = None
        # Variant by variant, memory cleared before each: otherwise the EN
        # answer to a question could be recalled while asking it in German.
        for variant in [v for v in VARIANT_LABEL if v in {q["variant"] for q in queries}]:
            todo = [q for q in queries if q["variant"] == variant and q["key"] not in done]
            if not todo:
                continue
            await k.clear_memory()
            print(f"query: {variant} – {VARIANT_LABEL[variant]} ({len(todo)} Fragen)")
            progress = Progress(len(todo), variant)

            async def one(q: dict) -> None:
                nonlocal model
                async with sem:
                    t0 = time.monotonic()
                    try:
                        res = compact_result(await k.ask(q["query"]))
                        model = model or res.get("model")
                    except Exception as exc:  # noqa: BLE001
                        res = {"error": f"{type(exc).__name__}: {exc}"}
                    res.update({"key": q["key"], "wall_ms": round((time.monotonic() - t0) * 1000)})
                    append_jsonl(results_path, res)
                    progress.tick(
                        "FEHLER"
                        if res.get("error")
                        else f"{res.get('compliance_verdict', '')} "
                        f"{(res.get('processing_ms') or 0) / 1000:.0f}s"
                    )

            await asyncio.gather(*(one(q) for q in todo))
        if model:
            config = load_config(run_dir)
            config["kermit_model"] = model
            save_config(run_dir, config)
    finally:
        await k.aclose()


def latest_results(run_dir: Path) -> dict[str, dict]:
    """Last result per key – a retried question supersedes its failed attempt."""
    out: dict[str, dict] = {}
    for r in read_jsonl(run_dir / "results.jsonl"):
        if r["key"] not in out or not r.get("error"):
            out[r["key"]] = r
    return out


async def phase_score(s: Settings, run_dir: Path) -> None:
    queries = read_jsonl(run_dir / "queries.jsonl")
    results = latest_results(run_dir)
    scored_path = run_dir / "scored.jsonl"
    previous = {r["key"]: r for r in read_jsonl(scored_path)}

    try:
        judge = Judge(s) if s.judge_enabled else None
    except JudgeConfigError as exc:
        sys.exit(f"score: {exc}")
    if not judge:
        print(
            "score: kein Judge konfiguriert (BENCH_JUDGE_MODEL) – nur deterministische Kennzahlen"
        )
    rows: list[dict] = []
    todo = [q for q in queries if q["key"] in results]
    progress = Progress(len(todo), "score")
    judge_failures = 0

    async def one(q: dict) -> dict:
        nonlocal judge_failures
        res = results[q["key"]]
        row = {**{k: v for k, v in q.items() if k != "support"}, **res}
        if res.get("error"):
            progress.tick("übersprungen (Fehler)")
            return row
        row.update(score_answer(q, res))
        old = previous.get(q["key"])
        if (
            judge
            and old
            and old.get("request_id") == res.get("request_id")
            and old.get("judge_model") == judge.model
            and "judge_error" not in old
        ):
            # Same answer, same judge: reuse the verdict instead of paying again.
            row.update(
                {
                    k: v
                    for k, v in old.items()
                    if k.startswith(("judge_", "fact", "extra_", "contradictions", "hallucinated"))
                }
            )
        elif judge:
            answer = strip_citations(res.get("answer", ""))
            try:
                if q["expect_answer"]:
                    facts = await judge.extract_facts(
                        f"{s.split}:{q['id']}:{q['lang']}", q["query"], q["gold"]
                    )
                    row.update(await judge.grade(q["query"], q["gold"], facts, answer))
                else:
                    row.update(await judge.grade_refusal(q["query"], answer))
                row["judge_model"] = judge.model
            except JudgeConfigError:
                raise
            except JudgeError as exc:
                judge_failures += 1
                row["judge_error"] = str(exc)
        progress.tick(str(row.get("judge_verdict") or row.get("judge_refused", "")))
        return row

    try:
        rows = list(await asyncio.gather(*(one(q) for q in todo)))
    except JudgeConfigError as exc:
        sys.exit(
            f"score: {exc}\nBENCH_JUDGE_* in benchmark/.env prüfen – oder "
            "BENCH_JUDGE_MODEL= leer setzen für Kennzahlen ohne Judge."
        )
    finally:
        if judge:
            await judge.aclose()
    write_jsonl(scored_path, rows)
    config = load_config(run_dir)
    config["judge_model"] = f"{judge.provider}:{judge.model}" if judge else None
    save_config(run_dir, config)
    missing = len(queries) - len(todo)
    print(
        f"score: {len(rows)} bewertet → {scored_path}"
        + (f", {missing} ohne Antwort (query erneut ausführen)" if missing else "")
        + (f", {judge_failures} Judge-Fehler" if judge_failures else "")
    )


async def phase_clean(s: Settings) -> None:
    k = Kermit(s)
    try:
        info = await k.remove()
    finally:
        await k.aclose()
    files = info.get("files", f"FEHLER ({info.get('files_error')})")
    print(
        f"clean: Use Case `{s.use_case}` "
        f"{'gelöscht' if info['use_case_deleted'] else 'war nicht (mehr) vorhanden'}; "
        f"{info['documents']} Dokumente, Collections {info['collections']}, "
        f"{files} gespeicherte Dateien entfernt.\n"
        "Bleibt: das Abfrage-Log in Postgres (Traces zu den request_ids im Bericht). "
        "Die Läufe unter benchmark/runs/ bleiben ebenfalls."
    )


def phase_report(s: Settings, run_dir: Path) -> None:
    rows = read_jsonl(run_dir / "scored.jsonl")
    if not rows:
        sys.exit("report: keine bewerteten Ergebnisse – zuerst `score` ausführen.")
    summary = write_report(run_dir, rows, load_config(run_dir))
    print(f"\nreport: {run_dir / 'summary.md'}\n")
    for v, a in summary["variants"].items():
        if v == "neg":
            print(f"  {v:6} abgelehnt {_pct(a.get('refusal_rate'))}  n={a['n']}")
        else:
            print(
                f"  {v:6} Judge {_pct(a.get('judge_score'))}  Fakten {_pct(a.get('fact_recall'))}  "
                f"F1 {_pct(a.get('f1'))}  Recall@5 {_pct(a.get('recall@5'))}  "
                f"Länge {a.get('length_ratio_median') or 0:.1f}×  n={a['n']}"
            )


def _pct(v) -> str:
    return "   –  " if v is None else f"{v * 100:5.1f}%"


# ── CLI ─────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("phase", choices=[CHECK] + PHASES + ["all", CLEAN])
    p.add_argument(
        "--run", type=Path, help="Laufverzeichnis (Standard: neuester Lauf; bei all/prepare: neu)"
    )
    p.add_argument(
        "--fresh", action="store_true", help="Index des Benchmark-Use-Case vorher leeren"
    )
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="nach dem Bericht alle Benchmark-Daten aus Kermit löschen (wie `clean`)",
    )
    p.add_argument("--split", choices=["test", "dev"])
    p.add_argument("--mono", type=int)
    p.add_argument("--multi", type=int)
    p.add_argument("--negative", type=int)
    p.add_argument(
        "--distractors",
        type=int,
        metavar="N",
        help="Ablenk-Artikel pro Frage im Index (Standard: alle, meist 6)",
    )
    p.add_argument("--variants", type=lambda v: [x.strip() for x in v.split(",") if x.strip()])
    p.add_argument("--seed", type=int)
    p.add_argument("--concurrency", dest="query_concurrency", type=int)
    args = p.parse_args()

    s = load_settings(
        {
            k: getattr(args, k)
            for k in (
                "split",
                "mono",
                "multi",
                "negative",
                "distractors",
                "variants",
                "seed",
                "query_concurrency",
            )
        }
    )
    unknown = set(s.variants) - set(VARIANT_LABEL)
    if unknown:
        sys.exit(f"Unbekannte Varianten {sorted(unknown)} – erlaubt: {', '.join(VARIANT_LABEL)}")

    if args.phase in (CLEAN, CHECK):
        try:
            asyncio.run(phase_clean(s) if args.phase == CLEAN else phase_check(s))
        except (KermitError, httpx.HTTPError) as exc:
            sys.exit(f"FEHLER: {exc}")
        return

    if args.phase in ("all", "prepare"):
        run_dir = args.run or RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        run_dir = args.run or latest_run()
        # A run keeps the use case it was prepared for.
        s.use_case = load_config(run_dir).get("use_case", s.use_case)

    phases = [CHECK] + PHASES if args.phase == "all" else [args.phase]
    try:
        for phase in phases:
            if phase == CHECK:
                asyncio.run(phase_check(s))
            elif phase == "prepare":
                phase_prepare(s, run_dir)
            elif phase == "setup":
                asyncio.run(phase_setup(s, run_dir, fresh=args.fresh))
            elif phase == "ingest":
                asyncio.run(phase_ingest(s, run_dir))
            elif phase == "query":
                asyncio.run(phase_query(s, run_dir))
            elif phase == "score":
                asyncio.run(phase_score(s, run_dir))
            elif phase == "report":
                phase_report(s, run_dir)
        if args.cleanup:
            asyncio.run(phase_clean(s))
    except KermitError as exc:
        sys.exit(f"FEHLER: {exc}")
    except KeyboardInterrupt:
        sys.exit(f"\nAbgebrochen. Fortsetzen mit: python benchmark/run.py <phase> --run {run_dir}")


if __name__ == "__main__":
    main()

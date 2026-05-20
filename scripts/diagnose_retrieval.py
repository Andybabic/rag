#!/usr/bin/env python3
"""Retrieval diagnostic – localises why an obviously-relevant passage is not
retrieved. Decides between two mutually exclusive causes:

  (A) the embedding model is degenerate (similar sentences ≈ orthogonal)
  (B) the passage is not in the index in a retrievable form
      (extraction / chunking dropped or garbled it)

Dependency-free (stdlib only). Run from the host with the service ports
mapped, or from inside the docker network.

  python3 scripts/diagnose_retrieval.py \
      --embed-url  http://localhost:8003 \
      --vectordb-url http://localhost:8004 \
      --collection wl_default
"""

from __future__ import annotations

import argparse
import json
import math
import os
import urllib.request

# The passage the user expects to find (Signalvorschrift §11 Ersatzsignal).
TARGET = "Blinkt am Armaturenpult die weiße Drucktaste Ersatzsignal"
PARAPHRASE = "Was muss ich tun, wenn die Drucktaste Ersatzsignal blinkt?"
UNRELATED = "Die Katze schläft auf dem Sofa in der Mittagssonne."
KEYWORDS = ("ersatzsignal", "armaturenpult", "drucktaste")


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        return json.load(r)


def embed(base: str, text: str) -> list[float]:
    return _post(
        f"{base.rstrip('/')}/v1/embed",
        {"type": "text", "content": text, "metadata": {}},
    )["vector"]


def cosine(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb + 1e-12)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--embed-url",
        default=os.getenv("EMBEDDING_SERVICE_URL", "http://localhost:8003"),
    )
    ap.add_argument(
        "--vectordb-url",
        default=os.getenv("VECTORDB_SERVICE_URL", "http://localhost:8004"),
    )
    ap.add_argument("--collection", default="wl_default")
    ap.add_argument("--top-k", type=int, default=50)
    args = ap.parse_args()

    print("=" * 70)
    print("STEP 1 — Embedding sanity (is the model degenerate?)")
    print("=" * 70)
    v_target = embed(args.embed_url, TARGET)
    v_para = embed(args.embed_url, PARAPHRASE)
    v_unrel = embed(args.embed_url, UNRELATED)
    sim = cosine(v_target, v_para)
    unrel = cosine(v_target, v_unrel)
    print(f"  dim                          : {len(v_target)}")
    print(f"  cos(similar)   should be HIGH : {sim:.4f}   (>0.5 = gesund)")
    print(f"  cos(unrelated) should be LOW  : {unrel:.4f}")
    embedding_healthy = sim > 0.5 and sim - unrel > 0.2

    print()
    print("=" * 70)
    print("STEP 2a — Collection size (alle Collections im Vektor-Index)")
    print("=" * 70)
    try:
        req = urllib.request.Request(
            f"{args.vectordb_url.rstrip('/')}/v1/collections"
        )
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            cols = json.load(r).get("collections", [])
        for c in cols:
            marker = "  <-- Ziel" if c.get("name") == args.collection else ""
            print(
                f"  {c.get('name'):30}  count={c.get('count')}"
                f"  dim={c.get('dimension')}  embed_model={c.get('embed_model')}"
                f"{marker}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  Collections-Endpoint nicht abrufbar: {exc}")

    print()
    print("=" * 70)
    print("STEP 2b — Is the target passage retrievable from the index?")
    print("=" * 70)
    try:
        res = _post(
            f"{args.vectordb_url.rstrip('/')}/v1/search",
            {
                "collection": args.collection,
                "vector": v_target,
                "top_k": args.top_k,
            },
        )
        hits = res.get("results", [])
    except Exception as exc:  # noqa: BLE001
        print(f"  Suche fehlgeschlagen: {exc}")
        hits = []

    print(f"  Treffer: {len(hits)} (top_k={args.top_k})")
    top_scores = [round(h.get("score", 0.0), 5) for h in hits[:5]]
    print(f"  Top-5 Scores: {top_scores}")

    # Show WHAT is actually in the index – which documents/pages survived.
    print(f"\n  Top-{min(len(hits), 10)} Chunks (was tatsächlich im Index liegt):")
    seen_files: dict[str, int] = {}
    for i, h in enumerate(hits[:10], 1):
        meta = h.get("metadata", {}) or {}
        fname = meta.get("file_name", "?")
        page = meta.get("page")
        text = (h.get("text") or meta.get("text") or "").replace("\n", " ")
        snippet = text[:140].strip()
        print(f"   [{i}] {fname} S.{page} score={h.get('score'):.4f}")
        print(f"       {snippet}")
    # Per-file count over the returned set
    for h in hits:
        fname = (h.get("metadata", {}) or {}).get("file_name", "?")
        seen_files[fname] = seen_files.get(fname, 0) + 1
    if seen_files:
        print(f"\n  Dateien in den Treffern: {dict(seen_files)}")
    found_rank = None
    for i, h in enumerate(hits, 1):
        text = (h.get("text") or h.get("metadata", {}).get("text") or "").lower()
        if all(k in text for k in KEYWORDS) or "armaturenpult" in text and "ersatzsignal" in text:
            found_rank = i
            print(f"  ✓ Zielpassage gefunden auf Rang {i} "
                  f"(score={h.get('score'):.5f}, "
                  f"S.{h.get('metadata', {}).get('page')})")
            break
    if found_rank is None:
        any_kw = any(
            any(k in (h.get("text") or "").lower() for k in KEYWORDS)
            for h in hits
        )
        print(f"  ✗ Zielpassage NICHT in den Top-{args.top_k}.")
        print(f"    (Teil-Keyword irgendwo vorhanden: {any_kw})")

    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    if not embedding_healthy:
        print("  → URSACHE (A): EMBEDDING DEGENERIERT.")
        print("    Selbst quasi-identische Sätze sind nicht ähnlich.")
        print("    qwen3-embedding:8b über Ollama liefert unbrauchbare")
        print("    Vektoren — Modell-Deployment/Endpoint prüfen. Index-")
        print("    Inhalt ist hier zweitrangig.")
    elif found_rank is None:
        print("  → URSACHE (B): EXTRAKTION / CHUNKING.")
        print("    Embedding ist gesund, aber die Zielpassage ist im")
        print("    Index nicht (sauber) vorhanden. §11 (Prosa + Tabelle)")
        print("    von Signalvorschrift-U-Bahn_2024.pdf wurde beim Ingest")
        print("    nicht/fehlerhaft extrahiert oder ungünstig gechunkt.")
    elif found_rank > 7:
        print(f"  → GRENZFALL: Passage existiert (Rang {found_rank}), fällt")
        print("    aber aus den Top-7. Reranker/Hybrid-Tuning nötig.")
    else:
        print("  → Retrieval sieht gesund aus — Problem woanders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

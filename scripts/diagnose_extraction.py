#!/usr/bin/env python3
"""Extraction diagnostic – distinguishes the two sub-causes of (B).

Sends a PDF through the cleaning service (the raw extraction, BEFORE
chunking/filtering) and checks whether the expected passage survives.

  (B1) extraction itself can't read it  → keywords absent in raw text
       (scanned/image PDF or PyMuPDF mangles tables; needs MinerU/OCR)
  (B2) extraction is fine, the chunker drops it → keywords PRESENT in raw
       text but the index has them not (boilerplate/low-alpha filter)

Stdlib only.

  python3 scripts/diagnose_extraction.py \
      --clean-url http://localhost:8001 \
      ~/path/to/Signalvorschrift-U-Bahn_2024.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
import uuid

KEYWORDS = ("Ersatzsignal", "Armaturenpult", "Drucktaste")
PAGE_MARK_RE = re.compile(r"<!--\s*[Pp]age\s*[:.]?\s*(\d+)\s*-->")


def post_pdf(clean_url: str, path: str) -> dict:
    boundary = f"----diag{uuid.uuid4().hex}"
    fname = os.path.basename(path)
    with open(path, "rb") as fh:
        data = fh.read()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode(),
        b"Content-Type: application/pdf\r\n\r\n",
        data,
        f"\r\n--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="config"\r\n\r\n',
        b'{"extract_images": false}',
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{clean_url.rstrip('/')}/v1/clean",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="Path to the PDF (e.g. Signalvorschrift-U-Bahn_2024.pdf)")
    ap.add_argument(
        "--clean-url",
        default=os.getenv("CLEANING_SERVICE_URL", "http://localhost:8001"),
    )
    args = ap.parse_args()

    res = post_pdf(args.clean_url, args.pdf)
    md = res.get("markdown", "") or ""
    pages = res.get("pages", []) or []
    low = md.lower()

    print("=" * 66)
    print("RAW EXTRACTION (cleaning service, vor Chunking/Filter)")
    print("=" * 66)
    print(f"  Seiten mit Text (pages-Liste) : {len(pages)}")
    print(f"  Textlänge md_content gesamt   : {len(md)} Zeichen")
    hits = {k: low.count(k.lower()) for k in KEYWORDS}
    for k, n in hits.items():
        print(f"  '{k}': {n}x")

    # Per-page breakdown derived from MinerU's <!-- Page N --> markers in md.
    marks = [(m.start(), int(m.group(1))) for m in PAGE_MARK_RE.finditer(md)]
    print(f"\n  Page-Marker im md_content : {len(marks)}")
    if marks:
        marks_sorted = sorted(marks, key=lambda x: x[0])
        ends = [m[0] for m in marks_sorted[1:]] + [len(md)]
        print(f"  {'page':>5}  {'chars':>6}  {'first 80 chars':<60}  ersatz")
        for (start, pno), end in zip(marks_sorted, ends):
            seg = md[start:end]
            preview = re.sub(r"\s+", " ", seg)[:80]
            has_e = "ersatzsignal" in seg.lower()
            print(
                f"  {pno:>5}  {len(seg):>6}  {preview:<60}  "
                f"{'★' if has_e else ''}"
            )

    # Show the context around the first Ersatzsignal mention, if any.
    idx = low.find("ersatzsignal")
    if idx != -1:
        snippet = md[max(0, idx - 120): idx + 200].replace("\n", " ")
        print(f"\n  Kontext: …{snippet}…")

    print("\n" + "=" * 66)
    print("VERDICT")
    print("=" * 66)
    if len(md) < 2000:
        print("  → (B1) EXTRAKTION LIEFERT FAST NICHTS.")
        print("    PDF ist vermutlich gescannt/Bild oder PyMuPDF scheitert.")
        print("    Fix: USE_MINERU=true (Layout+OCR+Tabellen) bzw. OCR-Fallback.")
    elif all(v == 0 for v in hits.values()):
        print("  → (B1) TEXT DA, ABER §11-INHALT FEHLT IN DER EXTRAKTION.")
        print("    Die Ersatzsignal-Passage/Tabelle wird von PyMuPDF nicht")
        print("    erfasst (Tabelle/Bild). Fix: USE_MINERU=true.")
    else:
        print("  → (B2) EXTRAKTION OK — der CHUNKER verwirft es.")
        print("    Keywords sind im Rohtext vorhanden, aber nicht im Index.")
        print("    Ursache: _is_boilerplate (alpha/total < 0.3 → Tabellen-")
        print("    chunks gelöscht) bzw. Header/Footer-Stripping zu aggressiv.")
        print("    Fix: Boilerplate-Heuristik im data_structure-Chunker lockern.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

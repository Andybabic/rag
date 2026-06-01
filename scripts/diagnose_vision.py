#!/usr/bin/env python3
"""Diagnose ob das konfigurierte Vision-Modell Bilder wirklich sieht.

Liest .env, fragt /api/show nach den Capabilities des VISION_MODEL und
schickt anschließend zwei bekannte Test-Bilder durch /api/chat:

  1. ein 200×200 px reines ROT-Quadrat
  2. ein 200×200 px reines BLAU-Quadrat

Wenn das Modell beide Male "rot" bzw. "blau" sagt, sieht es Bilder.
Wenn beide Antworten gleich klingen oder generische Logo-Beschreibungen
liefern, ignoriert es die Bilder.

Aufruf vom Repo-Root:
    python3 scripts/diagnose_vision.py
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def make_solid_png(color: tuple[int, int, int], size: int = 64) -> bytes:
    """Erzeuge ein einfarbiges PNG nur mit stdlib (zlib + struct).

    Kein Pillow nötig, läuft auch im nackten cleaning-Container.
    """
    r, g, b = color
    row = b"\x00" + bytes([r, g, b]) * size  # filter byte + RGB pixels
    raw = row * size
    idat = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        signature
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def post_json(url: str, payload: dict, headers: dict, timeout: int = 90) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    cfg = read_env(ENV_FILE)
    base_url = cfg.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    api_key = cfg.get("OLLAMA_API_KEY", "")
    model = cfg.get("VISION_MODEL", "qwen3:8b")
    num_ctx = int(cfg.get("OLLAMA_NUM_CTX", "32768") or "32768")

    print(f"Endpoint : {base_url}")
    print(f"Modell   : {model}")
    print(f"num_ctx  : {num_ctx}")
    print()

    headers = {"X-API-Key": api_key} if api_key else {}

    # ── 1. /api/show: Capabilities ───────────────────────────────
    print("── /api/show ────────────────────────────────────────")
    try:
        info = post_json(
            f"{base_url}/api/show",
            {"model": model},
            headers,
            timeout=20,
        )
    except (HTTPError, URLError) as exc:
        print(f"FEHLER beim /api/show: {exc}")
        return 1

    capabilities = info.get("capabilities") or []
    families = (info.get("details") or {}).get("families") or []
    print(f"capabilities: {capabilities or '(leer)'}")
    print(f"families    : {families}")
    has_vision = (
        "vision" in [c.lower() for c in capabilities]
        or any("clip" in f.lower() or "vl" in f.lower() for f in families)
    )
    if has_vision:
        print("→ Vision laut Metadata: JA")
    else:
        print(
            "→ Vision laut Metadata: NEIN/UNBEKANNT — Bilder werden "
            "wahrscheinlich ignoriert."
        )
    print()

    # ── 2. Probe mit ROT / BLAU ──────────────────────────────────
    prompt = (
        "Welche EINE Hauptfarbe siehst du auf dem Bild? "
        "Antworte nur mit einem Wort: rot, blau, grün, gelb, schwarz oder weiß."
    )
    cases = [("ROT", (220, 30, 30)), ("BLAU", (30, 60, 220))]
    answers: list[str] = []
    for label, rgb in cases:
        png_b64 = base64.b64encode(make_solid_png(rgb)).decode("ascii")
        try:
            resp = post_json(
                f"{base_url}/api/chat",
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                            "images": [png_b64],
                        }
                    ],
                    "stream": False,
                    "options": {"num_ctx": num_ctx, "temperature": 0.0},
                },
                headers,
            )
        except (HTTPError, URLError) as exc:
            print(f"{label}-Probe: FEHLER {exc}")
            return 1
        answer = (
            (resp.get("message") or {}).get("content", "").strip().lower()
        )
        answers.append(answer)
        print(f"{label}-Bild → '{answer}'")

    print()
    rot_ok = "rot" in answers[0]
    blau_ok = "blau" in answers[1]
    if rot_ok and blau_ok:
        print(
            "✅ Modell unterscheidet die Bilder korrekt — Vision funktioniert."
        )
        return 0
    if answers[0] == answers[1]:
        print(
            "❌ Beide Bilder bekamen die gleiche Antwort — das Modell sieht "
            "die Bilder nicht und antwortet nur auf den Text-Prompt.\n"
            "   → Es ist effektiv text-only. Wechsel auf ein VL-Modell "
            "(z.B. llava, minicpm-v, qwen2.5vl) ist nötig."
        )
        return 3
    print(
        "⚠️  Modell antwortet variabel, aber nicht passend. Eventuell "
        "halbgares Vision-Modell oder zu langer/komplexer Prompt."
    )
    return 4


if __name__ == "__main__":
    sys.exit(main())

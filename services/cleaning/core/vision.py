"""Generate alt-text descriptions for images via the central LLM pipeline.

Provider, model and the prompt itself are resolved per use-case (DB) and
fall back to env / hardcoded defaults when nothing is configured.
"""

from __future__ import annotations

import logging
import re

from config import settings
from shared.llm import LLMUnavailableError, vision_describe
from shared.usecase_config import resolve_config, resolve_prompt

logger = logging.getLogger(__name__)

PROMPT_KEY_VISION = "vision.alt_text"

_DEFAULT_PROMPT = (
    "Beschreibe was auf dem Bild zu sehen ist: konkrete Formen, "
    "Beschriftungen, Symbole, Tabellenwerte, Logos. Schreibe 1-2 kurze "
    "deutsche Sätze. Erfinde nichts. Wenn das Bild ein Logo, Stempel oder "
    "reines Deko-Element ist, sage das (z.B. \"Firmenlogo Wiener Linien\"). "
    "Antworte als reiner Text — keine Markdown-Syntax, keine ![]() Wrapper, "
    "keine Anführungszeichen, kein Rahmentext."
)

# Modelle wickeln ihre Antwort manchmal in Markdown-Bildsyntax ![alt](url)
# oder eine fenced code block. Wir wollen nur den reinen alt-Text.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_FENCE_RE = re.compile(r"^```[a-z]*\n?|```$", re.MULTILINE)

# Semantischer Filter für den an die Vision gesendeten Vor/Nach-Kontext.
# Wenn da TOC-Einträge, Seiten-Footer oder URLs landen, paraphrasiert
# das Vision-Modell den Bumf statt das Bild zu beschreiben.
_TOC_LINE_RE = re.compile(r"\.{2,}\s*\d*\s*$")  # "Kapitel..... 5" / "...... "
_TOC_PREFIX_RE = re.compile(r"^\s*\d+(\.\d+)*\.?\s+\S")  # "1.2.3 Foo"
_PAGE_MARK_RE = re.compile(r"^\s*(seite|page|s\.?)\s+\d+", re.IGNORECASE)
_VERSION_RE = re.compile(r"\bversion\s*[:=]?\s*\d", re.IGNORECASE)
_URL_RE = re.compile(r"https?://|www\.")
_DOC_NAME_RE = re.compile(r"\.(docx|pdf|doc|xlsx?)\b", re.IGNORECASE)
_ALPHA_RE = re.compile(r"[A-Za-zÄÖÜäöüß]")
_PAGE_ANCHOR_RE = re.compile(r"<!--\s*page[:\s]+\d+\s*-->", re.IGNORECASE)


def _filter_context(text: str, max_chars: int = 250) -> str:
    """Keep only flowing prose, drop boilerplate (TOC, page footers,
    headers, URLs, all-caps ribbons). Truncate to ``max_chars``."""
    if not text:
        return ""
    cleaned = _PAGE_ANCHOR_RE.sub(" ", text)
    out: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _PAGE_MARK_RE.match(line):
            continue
        if _TOC_LINE_RE.search(line):
            continue
        if _TOC_PREFIX_RE.match(line) and (
            _TOC_LINE_RE.search(line) or len(line.split()) <= 6
        ):
            continue
        if _URL_RE.search(line):
            continue
        if _DOC_NAME_RE.search(line) and _VERSION_RE.search(line):
            continue
        # nur Versalien / Pipe-getrennte Ribbons wie "A|B|C"
        letters = _ALPHA_RE.findall(line)
        if letters and all(c.isupper() for c in letters) and len(line) < 80:
            continue
        # Single-Token-Marker (z.B. "l)", "a)") raus, aber Section-
        # Headlines wie "Änderungsverzeichnis" / "Anwendungsbereich"
        # behalten — die geben dem Vision-Modell den semantischen Hook
        # zur Verortung im Dokument.
        if len(line) < 4:
            continue
        out.append(line)
    joined = " ".join(out).strip()
    if len(joined) > max_chars:
        joined = joined[:max_chars].rsplit(" ", 1)[0] + " …"
    return joined


def _clean_alt_text(raw: str) -> str:
    if not raw:
        return ""
    text = raw.strip()
    # ![alt-text](url) → nur den alt-text behalten
    m = _MD_IMAGE_RE.fullmatch(text)
    if m:
        text = m.group(1)
    else:
        text = _MD_IMAGE_RE.sub(lambda m: m.group(1), text)
    text = _FENCE_RE.sub("", text).strip()
    # Doppelte umgebende Anführungszeichen entfernen
    if len(text) >= 2 and text[0] in {'"', "'", "„"} and text[-1] in {'"', "'", "“"}:
        text = text[1:-1].strip()
    return text


async def generate_alt_text(
    base64_image: str,
    context: str = "",
    *,
    use_case: str = "",
    text_before: str = "",
    text_after: str = "",
) -> str:
    """Send a base64 image to the multimodal LLM and return alt-text.

    Args:
        base64_image: Base64-encoded image data (without data URI prefix).
        context: Fallback surrounding-page context (used only when neither
            ``text_before`` nor ``text_after`` is provided).
        use_case: Use case id; selects per-usecase prompt + model overrides.
        text_before: Document text that immediately precedes the image.
        text_after: Document text that immediately follows the image.

    Returns:
        Alt-text description, or empty string on failure.
    """
    base_prompt = await resolve_prompt(
        use_case or "*", PROMPT_KEY_VISION, default=_DEFAULT_PROMPT
    ) or _DEFAULT_PROMPT
    # Kontext semantisch filtern: TOC-Einträge, Seiten-Footer, URLs,
    # Versalien-Ribbons (z.B. "WIENERLINIEN|WIENENERGIE|...") landen
    # sonst im Prompt und verleiten das Modell, sie zu paraphrasieren
    # statt das Bild zu beschreiben. Bleibt nach dem Filter nichts
    # übrig, schicken wir lieber gar keinen Kontext.
    before_clean = _filter_context(text_before, max_chars=250)
    after_clean = _filter_context(text_after, max_chars=250)
    page_clean = (
        _filter_context(context, max_chars=250)
        if not (before_clean or after_clean)
        else ""
    )

    ctx_parts: list[str] = []
    if before_clean:
        ctx_parts.append(f"Davor stand: {before_clean}")
    if after_clean:
        ctx_parts.append(f"Danach steht: {after_clean}")
    if page_clean:
        ctx_parts.append(f"Auf derselben Seite: {page_clean}")

    if ctx_parts:
        prompt = (
            f"{base_prompt}\n\n"
            "Der folgende Text ist der Dokumentkontext rund um das Bild — "
            "NICHT der Bildinhalt. Beschreibe also nicht den Text. Aber: "
            "wenn der Kontext klar einordnet, was das Bild im Dokument "
            "darstellt (Kapitel-Überschrift, Tabellenbezug, Diagrammtitel), "
            "ergänze nach der Bildbeschreibung EINEN kurzen Verortungs-Satz, "
            "z.B. \"Gehört zum Abschnitt Änderungsverzeichnis.\" oder "
            "\"Stellt die Abkürzungstabelle des QSU-Management-Systems dar.\" "
            "Verortung nur wenn eindeutig — sonst weglassen.\n"
            + "\n".join(ctx_parts)
        )
    else:
        prompt = base_prompt

    cfg = await resolve_config(use_case or None)
    model = cfg.vision_model or settings.VISION_MODEL

    try:
        raw = await vision_describe(
            prompt,
            base64_image,
            model=model,
            config=cfg,
        )
    except LLMUnavailableError as exc:
        logger.warning("Alt-text generation failed: %s", exc)
        return ""
    return _clean_alt_text(raw)


async def enrich_images_with_alt_text(
    images: list[dict],
    pages: list[dict],
    *,
    use_case: str = "",
) -> list[dict]:
    """Generate alt-text for each image and add it to the image dict.

    Also returns the images enriched with an ``alt_text`` field.
    """
    if not images:
        return images

    enriched = []
    for img in images:
        base64_data = img.get("base64", "")
        if not base64_data:
            enriched.append(img)
            continue

        text_before = img.get("text_before", "")
        text_after = img.get("text_after", "")

        # Fallback to current-page text only when MinerU didn't give us
        # adjacent items (e.g. a page with just one image and no prose).
        context = ""
        if not (text_before or text_after):
            page_num = img.get("page", 1)
            for p in pages:
                if p.get("page") == page_num:
                    context = p.get("text", "")[:300]
                    break

        alt_text = await generate_alt_text(
            base64_data,
            context,
            use_case=use_case,
            text_before=text_before,
            text_after=text_after,
        )
        enriched.append({**img, "alt_text": alt_text})

    return enriched

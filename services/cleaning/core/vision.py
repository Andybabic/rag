"""Generate alt-text descriptions for images via the central LLM pipeline.

Backend (Ollama / OpenAI) is selected via ``VISION_PROVIDER`` env var.
"""

from __future__ import annotations

import logging

from config import settings
from shared.llm import LLMUnavailableError, vision_describe

logger = logging.getLogger(__name__)

_PROMPT = (
    "Beschreibe dieses Bild präzise und sachlich in 1-3 Sätzen auf Deutsch. "
    "Konzentriere dich auf technisch relevante Inhalte: Diagramme, Schaltpläne, "
    "Maschinenteile, Tabellen, Beschriftungen. "
    "Gib NUR die Beschreibung zurück, keinen Rahmentext."
)


async def generate_alt_text(base64_image: str, context: str = "") -> str:
    """Send a base64 image to the multimodal LLM and return alt-text.

    Args:
        base64_image: Base64-encoded image data (without data URI prefix).
        context: Optional surrounding text context for better descriptions.

    Returns:
        Alt-text description, or empty string on failure.
    """
    prompt = _PROMPT
    if context:
        prompt += f"\n\nKontext aus dem Dokument: {context[:300]}"

    try:
        return await vision_describe(
            prompt,
            base64_image,
            model=settings.VISION_MODEL,
        )
    except LLMUnavailableError as exc:
        logger.warning("Alt-text generation failed: %s", exc)
        return ""


async def enrich_images_with_alt_text(
    images: list[dict],
    pages: list[dict],
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

        # Find surrounding text for context
        page_num = img.get("page", 1)
        context = ""
        for p in pages:
            if p.get("page") == page_num:
                context = p.get("text", "")[:300]
                break

        alt_text = await generate_alt_text(base64_data, context)
        enriched.append({**img, "alt_text": alt_text})

    return enriched

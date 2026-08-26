"""Plugin enrichment must not delete the chunker's own metadata.

Every plugin rebuilds ``extra`` as ``{**raw_meta, ...}``. ``raw_meta`` is the
document-level dict, so that assignment dropped the keys the chunker derives
per chunk — breadcrumb and the image bindings. The images then never reached
Qdrant, which made every image feature downstream dead code: the answer side
found no images on any chunk and could never show one.
"""

from __future__ import annotations

import pytest

from core.chunker import chunk_markdown
from plugins import get_plugin

PARAGRAPH = (
    "Die Bedienung der NT-Anlage erfolgt am Fahrerplatz und ist in diesem "
    "Abschnitt ausfuehrlich beschrieben."
)

PAGE = (
    "<!-- Page 82 -->\n"
    "# 5.2.4 Ausstiegsseite\n\n"
    "<!-- image id:img_p082_i01 page:82 -->\n\n" + PARAGRAPH
)

IMAGES = [{
    "image_id": "img_p082_i01",
    "page": 82,
    "alt_text": "Taste 5 = links, Taste 0 = rechts",
    "url": "/api/images/img_p082_i01",
}]


def _chunk(use_case: str, collection: str, **extra):
    """Chunk *with the plugin wired in* — the router always passes one, and the
    bug under test only appears once a plugin has rewritten ``extra``."""
    return chunk_markdown(
        PAGE,
        file_name="ABU-BAs-004_15.pdf",
        doc_type="pdf",
        use_case=use_case,
        collection=collection,
        images=IMAGES,
        extra=extra or None,
        plugin=get_plugin(use_case),
    )


# Every use case that ships a plugin rebuilding `extra`.
PLUGGED_USE_CASES = [
    ("wiener_linien", "wl_default"),
    ("neumann", "neumann_machines"),
    ("gw_stpoelten", "gw_docs"),
]


@pytest.mark.parametrize("use_case,collection", PLUGGED_USE_CASES)
def test_images_survive_plugin_enrichment(use_case, collection):
    chunks = _chunk(use_case, collection)

    images = (chunks[0].metadata.extra or {}).get("images") or []
    assert [i["id"] for i in images] == ["img_p082_i01"]


@pytest.mark.parametrize("use_case,collection", PLUGGED_USE_CASES)
def test_breadcrumb_survives_plugin_enrichment(use_case, collection):
    """Its absence in the live index is what exposed the deletion."""
    chunks = _chunk(use_case, collection)

    assert (chunks[0].metadata.extra or {}).get("breadcrumb")


def test_alt_text_survives_so_retrieval_can_use_it():
    chunks = _chunk("wiener_linien", "wl_default")

    images = (chunks[0].metadata.extra or {}).get("images")
    assert images[0]["alt_text"] == "Taste 5 = links, Taste 0 = rechts"


def test_plugin_fields_are_still_applied():
    """The re-apply must not undo the enrichment it protects."""
    chunks = _chunk("wiener_linien", "wl_default")

    extra = chunks[0].metadata.extra or {}
    for key in ("audience", "difficulty", "law_ref", "criticality"):
        assert key in extra


def test_document_level_extra_is_still_carried():
    chunks = _chunk("wiener_linien", "wl_default", audience="trainee")

    assert (chunks[0].metadata.extra or {}).get("audience") == "trainee"


@pytest.mark.parametrize("use_case,_collection", PLUGGED_USE_CASES)
def test_plugins_still_wipe_extra_on_their_own(use_case, _collection):
    """Pins the reason the chunker re-applies its keys.

    Called directly, every plugin replaces ``extra`` wholesale from
    ``raw_meta``. That is why the guarantee lives in the chunker rather than in
    a convention each plugin has to remember — this test should keep passing;
    it documents plugin behaviour, it does not ask for it to change.
    """
    from shared.models import ChunkMetadata

    meta = ChunkMetadata(
        chunk_id="c1", file_name="f.pdf", page=82, total_pages=1,
        doc_type="pdf", use_case=use_case, collection="c",
        extra={"breadcrumb": "5.2.4", "images": [{"id": "img_a"}]},
    )

    enriched = get_plugin(use_case).enrich_metadata(meta, PARAGRAPH, {})

    assert "images" not in (enriched.extra or {})
    assert "breadcrumb" not in (enriched.extra or {})

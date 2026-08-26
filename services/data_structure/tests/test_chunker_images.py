"""Images bind to their page, not only to the chunk holding the anchor.

A page is split into several chunks, but the anchor sits in exactly one of
them. Retrieval that lands on any other chunk of the same page then comes back
without the graphic — even though the graphic is on that page of that manual,
and in technical documents it often carries the actual answer.
"""

from __future__ import annotations

from core.chunker import chunk_markdown

DEFAULTS = {
    "file_name": "ABU-BAs-004_15.pdf",
    "doc_type": "pdf",
    "use_case": "wiener_linien",
    "collection": "wl_default",
}


def _img(image_id: str, page: int, alt: str = "Taste 5 = links, Taste 0 = rechts"):
    return {"image_id": image_id, "page": page, "alt_text": alt, "url": f"/api/i/{image_id}"}


def _chunk(markdown: str, images=None):
    return chunk_markdown(markdown, images=images or [], **DEFAULTS)


def _images_of(chunk):
    return (chunk.metadata.extra or {}).get("images") or []


# chunk_markdown drops anything under 30 characters as boilerplate, so test
# fixtures need real paragraphs rather than placeholder strings.
PARAGRAPH = (
    "Die Bedienung der NT-Anlage erfolgt am Fahrerplatz und ist in diesem "
    "Abschnitt beschrieben."
)

LONG_PAGE = (
    "<!-- Page 82 -->\n"
    "# 5.2.4 Ausstiegsseite\n\n"
    "Die Bedienung der NT-Anlage erfolgt am Fahrerplatz. " * 30
    + "\n\n<!-- image id:img_p082_i01 page:82 -->\n\n"
    + "Weitere Hinweise zur Anlage folgen im naechsten Abschnitt. " * 30
)


def test_anchor_chunk_gets_the_image():
    chunks = _chunk(LONG_PAGE, [_img("img_p082_i01", 82)])

    assert any("img_p082_i01" in str(_images_of(c)) for c in chunks)


def test_every_chunk_of_the_page_gets_the_image():
    """This is the fix: a hit anywhere on page 82 must bring the graphic."""
    chunks = _chunk(LONG_PAGE, [_img("img_p082_i01", 82)])

    assert len(chunks) > 1, "test needs a page that splits into several chunks"
    for chunk in chunks:
        assert [i["id"] for i in _images_of(chunk)] == ["img_p082_i01"]


def test_images_of_other_pages_do_not_leak():
    md = LONG_PAGE + "\n\n<!-- Page 83 -->\n\nText auf Seite 83."
    chunks = _chunk(md, [_img("img_p082_i01", 82), _img("img_p083_i00", 83)])

    for chunk in chunks:
        ids = [i["id"] for i in _images_of(chunk)]
        expected = "img_p082_i01" if chunk.metadata.page == 82 else "img_p083_i00"
        assert ids == [expected]


def test_anchored_image_is_listed_first():
    """Downstream offers them to the model in this order, so the graphic that
    sits in this very chunk should lead."""
    md = (
        "<!-- Page 82 -->\n\n"
        "<!-- image id:img_b page:82 -->\n\n"
        + PARAGRAPH
    )
    chunks = _chunk(md, [_img("img_a", 82), _img("img_b", 82)])

    assert [i["id"] for i in _images_of(chunks[0])] == ["img_b", "img_a"]


def test_no_duplicate_when_anchor_and_page_agree():
    chunks = _chunk(
        "<!-- Page 82 -->\n\n<!-- image id:img_p082_i01 page:82 -->\n\n" + PARAGRAPH,
        [_img("img_p082_i01", 82)],
    )

    assert len(_images_of(chunks[0])) == 1


def test_alt_text_travels_with_the_chunk():
    chunks = _chunk(LONG_PAGE, [_img("img_p082_i01", 82)])

    assert _images_of(chunks[0])[0]["alt_text"] == "Taste 5 = links, Taste 0 = rechts"


def test_pages_without_images_stay_clean():
    chunks = _chunk("<!-- Page 5 -->\n\n" + PARAGRAPH, [_img("x", 82)])

    assert _images_of(chunks[0]) == []


def test_image_with_unusable_page_is_ignored_for_page_binding():
    """A missing or non-numeric page must not crash ingest or attach the image
    to every page of the document."""
    chunks = _chunk(LONG_PAGE, [{"image_id": "broken", "page": None, "alt_text": "x"}])

    for chunk in chunks:
        assert _images_of(chunk) == []

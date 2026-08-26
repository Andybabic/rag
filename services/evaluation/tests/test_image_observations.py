"""Image descriptions must reach the agent, not just the synthesizer.

In technical manuals the answer regularly exists only inside a graphic — a key
assignment, a switch position, a table value — while the surrounding text says
nothing about it. An agent reading chunk text alone reports "not in the
documents" while the answer sits one layer down in the image description.
"""

from __future__ import annotations

from core.actions import _image_block


def _meta(*images: dict) -> dict:
    return {"extra": {"images": list(images)}}


def _img(img_id: str, alt: str, page: int = 82) -> dict:
    return {"id": img_id, "page": page, "alt_text": alt, "url": f"/api/images/{img_id}"}


def test_description_is_rendered_into_the_observation():
    block = _image_block(_meta(_img("img_p082_i01", "Taste 5 = links, Taste 0 = rechts")))

    assert "Taste 5 = links, Taste 0 = rechts" in block


def test_image_id_is_carried_so_the_answer_can_reference_it():
    block = _image_block(_meta(_img("img_p082_i01", "Bahnsteigseite")))

    assert "img_p082_i01" in block


def test_every_image_of_the_page_is_included():
    block = _image_block(_meta(
        _img("a", "Bedienfeld mit Tastenbelegung"),
        _img("b", "Notbremse rot"),
    ))

    assert "Bedienfeld mit Tastenbelegung" in block
    assert "Notbremse rot" in block


def test_images_without_a_description_are_skipped():
    """An empty alt-text means the vision job has not run yet; emitting a bare
    marker would spend context on nothing."""
    block = _image_block(_meta(_img("a", ""), _img("b", "   "), _img("c", "echt")))

    assert block.count("[BILD") == 1
    assert "echt" in block


def test_chunk_without_images_adds_nothing():
    assert _image_block({"extra": {}}) == ""
    assert _image_block({}) == ""


def test_long_transcription_is_not_truncated():
    """A labelled graphic transcribes into a long list of assignments. Cutting
    it drops exactly the values the question asks about."""
    alt = "; ".join(f"Taste {i} = Funktion {i}" for i in range(40))

    assert alt in _image_block(_meta(_img("a", alt)))

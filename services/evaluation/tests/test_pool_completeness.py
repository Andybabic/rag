"""The synthesizer can only cite what the chunk pool actually shows it.

An answer that is accurate but incomplete looks like a prompt problem and gets
treated as one. It was a data problem: the pool renderer cut every chunk at 400
characters while the chunker emits ~500, so the tail of each source — where a
section's consequence and constraint sentences sit — never reached the model.
The prompt rule that tells the synthesizer to exhaust its leading sources is
worthless while the sources arrive pre-truncated.
"""

from __future__ import annotations

from core.manager import (
    _POOL_CHUNK_CHARS,
    _format_global_chunks,
    _merge_chunks,
)

# Verbatim from ABU-BAs-087_03.pdf, S. 72 — 500 characters. The closing
# sentence about the arrows changing appearance is what a 400-char cut removed.
EXIT_SIDE_CHUNK = (
    "[Wagentype X Triebwagen U-Bahn | Auswahl der Ausstiegsseite]\n\n"
    "# Auswahl der Ausstiegsseite\n\n"
    "Die Ausstiegsseite der naechsten Station wird auf der Bedienoberflaeche "
    "durch einen aktiven linken bzw. rechten Pfeil neben der Haltestelle "
    "angezeigt. Das Fahrpersonal hat die Moeglichkeit, die Seite manuell durch "
    "Antippen der Pfeile zu aendern (notwendig im Gleiswechselbetrieb).\n\n"
    "# wird zu\n\n"
    "Aendert das Fahrpersonal manuell die Ausstiegsseite, aendert sich "
    "ebenfalls das Aussehen der Pfeile."
)


def _sub(chunks):
    return {"chunks": chunks}


def test_normal_chunk_survives_the_pool_intact():
    rendered = _format_global_chunks(
        [{"text": EXIT_SIDE_CHUNK, "metadata": {"file_name": "abu.pdf", "page": 72}}]
    )
    assert "aendert sich ebenfalls das Aussehen der Pfeile" in rendered


def test_pool_budget_covers_a_full_chunk():
    assert _POOL_CHUNK_CHARS > len(EXIT_SIDE_CHUNK)


def test_oversized_chunk_is_still_bounded():
    rendered = _format_global_chunks(
        [{"text": "x" * 9000, "metadata": {"file_name": "a.pdf", "page": 1}}]
    )
    assert rendered.count("x") == _POOL_CHUNK_CHARS


def test_pool_is_ordered_by_relevance_across_subagents():
    """Pool order is the citation numbering, so [1] must be the best chunk.

    The weak chunk is listed first here because its sub-agent ran first — the
    order the pool used to keep.
    """
    pool = _merge_chunks([
        _sub([{"text": "weak, adjacent function", "score": 0.05, "metadata": {}}]),
        _sub([{"text": "the chunk that answers it", "score": 0.98, "metadata": {}}]),
    ])
    assert pool[0]["text"] == "the chunk that answers it"
    assert _format_global_chunks(pool).startswith("[1] () the chunk that answers it")


def test_dedup_still_keeps_the_higher_scored_copy():
    pool = _merge_chunks([
        _sub([{"text": "same text", "score": 0.1, "metadata": {"file_name": "a.pdf"}}]),
        _sub([{"text": "same text", "score": 0.9, "metadata": {"file_name": "b.pdf"}}]),
    ])
    assert len(pool) == 1
    assert pool[0]["metadata"]["file_name"] == "b.pdf"


def test_missing_scores_do_not_crash_the_sort():
    pool = _merge_chunks([
        _sub([{"text": "no score", "metadata": {}}]),
        _sub([{"text": "scored", "score": 0.4, "metadata": {}}]),
    ])
    assert [c["text"] for c in pool] == ["scored", "no score"]


def test_citation_rules_precede_the_completeness_rules():
    """Order inside the rule list is not cosmetic.

    The completeness rules were first inserted above the citation block, and
    the next synthesis came back with no inline [n] at all — caught by the
    deterministic check, rewritten at the cost of a second synthesis call. One
    sample proves nothing on its own, but the rule that must never be crowded
    out is the one whose failure the pipeline has to repair afterwards.
    """
    from core.manager import _SYNTHESIZER_SYSTEM_TEMPLATE as tpl

    must_cite = tpl.index("Jede Tatsachenbehauptung muss mit [n] belegt sein")
    placement = tpl.index("PLATZIERUNG:")
    exhaust = tpl.index("SCHÖPFE DIE TRAGENDEN CHUNKS AUS")
    assert must_cite < exhaust
    assert placement < exhaust


def test_completeness_rule_ships_with_its_counterweight():
    """"Exhaust the sources" alone reads as "write more", which is how an
    answer picks up steps belonging to a different device."""
    from core.manager import _SYNTHESIZER_SYSTEM_TEMPLATE as tpl

    assert "SCHÖPFE DIE TRAGENDEN CHUNKS AUS" in tpl
    assert "KEINE Aufforderung zur Länge" in tpl
    assert "Übertrage niemals eine Eigenschaft" in tpl


def test_citation_requirement_closes_the_request():
    """The rule has to stand where generation starts, not only in the system
    prompt — the pool now sits between the two and dwarfs both.

    Asserted on the user message because that is what the ordering claim is
    about: the last thing the model reads before it writes.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    import core.manager as manager

    captured = {}

    async def fake_call_llm(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "Eine Aussage [1]."

    subagents = [{
        "role": "facts", "role_label": "Fakten", "sub_query": "q",
        "answer": "Fragment", "chunks": [
            {"text": f"chunk {i}", "score": 0.5, "metadata": {"file_name": "a.pdf"}}
            for i in range(3)
        ],
    }]
    plan = {"merge_strategy": "complementary", "rationale": "r"}

    with patch.object(manager, "call_llm", AsyncMock(side_effect=fake_call_llm)):
        asyncio.run(manager.synthesize(
            query="Wie?", plan=plan, subagents=subagents,
            use_case="wiener_linien", on_event=None,
        ))

    tail = captured["user"][-400:]
    assert "[n]" in tail
    assert "[1] bis [3]" in tail

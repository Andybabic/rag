"""A citation belongs next to the claim it supports.

Collected into a bibliography at the end it tells the reader nothing about
which statement rests on what — and it still contains [n], so a plain presence
check reads it as full coverage. That is how an answer without a single inline
reference passed compliance reporting eight citations.
"""

from __future__ import annotations

import pytest

import core.manager as manager
from core.manager import has_inline_citations

CHUNKS = [{"text": f"c{i}", "metadata": {"file_name": "sv-u.pdf"}} for i in range(8)]

BIBLIOGRAPHY = """Der Zug wird in der Wendeanlage umgerüstet.
Vor der Einfahrt ist eine Durchsage zu tätigen.

[1] DB-V47-801_Betriebsverfahren.pdf | 4.3. Wendeverfahren
[2] U-BahnfahrerInnen - ABU-BAs-004_15.pdf | 4.18.1. Wendeanlage"""

INLINE = """Der Zug wird in der Wendeanlage umgerüstet [4].
Vor der Einfahrt ist eine Durchsage zu tätigen [2]."""


def test_trailing_source_list_does_not_count_as_cited():
    assert has_inline_citations(BIBLIOGRAPHY) is False


def test_markers_next_to_statements_count():
    assert has_inline_citations(INLINE) is True


def test_inline_markers_survive_a_trailing_source_list():
    """Mixed output must not be rejected — the body is properly cited even if
    the model also appended a redundant list."""
    assert has_inline_citations(INLINE + "\n\n[2] datei.pdf | Abschnitt") is True


def test_answer_without_any_marker_is_uncited():
    assert has_inline_citations("Der Zug wird umgerüstet.") is False


def test_a_numbered_list_item_is_not_mistaken_for_a_source_line():
    """Enumerated steps start with "1." not "[1]", but a body line that opens
    with a marker and carries real prose is still a cited statement."""
    answer = "Ablauf:\n[3] Der Zug fährt in die Wendeanlage ein und wird umgerüstet."

    assert has_inline_citations(answer) is True


@pytest.mark.anyio
async def test_bibliography_only_answer_is_sent_back_for_rewrite(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("compliance LLM should not be reached")

    monkeypatch.setattr(manager, "call_llm", fail)

    result = await manager.check_compliance(
        answer=BIBLIOGRAPHY, global_chunks=CHUNKS,
        use_case_prompt="p", use_case="wl", on_event=None,
    )

    assert result["verdict"] == "REWRITE"


@pytest.mark.anyio
async def test_rewrite_guidance_asks_for_inline_placement(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("compliance LLM should not be reached")

    monkeypatch.setattr(manager, "call_llm", fail)

    result = await manager.check_compliance(
        answer=BIBLIOGRAPHY, global_chunks=CHUNKS,
        use_case_prompt="p", use_case="wl", on_event=None,
    )

    assert "direkt hinter die jeweilige Aussage" in result["guidance"]
    assert "Keine gesammelte Quellenliste" in result["guidance"]


@pytest.mark.anyio
async def test_properly_cited_answer_reaches_the_llm_checker(monkeypatch):
    seen: dict = {}

    async def fake(messages, **kwargs):
        seen["called"] = True
        return '{"verdict": "OK", "issues": [], "guidance": ""}'

    monkeypatch.setattr(manager, "call_llm", fake)

    await manager.check_compliance(
        answer=INLINE, global_chunks=CHUNKS,
        use_case_prompt="p", use_case="wl", on_event=None,
    )

    assert seen.get("called") is True


def test_synthesizer_prompt_forbids_a_source_list():
    assert "VERBOTEN" in manager._SYNTHESIZER_SYSTEM_TEMPLATE
    assert "PLATZIERUNG" in manager._SYNTHESIZER_SYSTEM_TEMPLATE

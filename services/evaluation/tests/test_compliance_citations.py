"""An answer with no [n] at all must never pass compliance.

"Citation-Coverage" is the compliance checker's first criterion, but the LLM
has returned OK for a 2387-character answer carrying zero citations. That
renders without a QUELLEN section, which reads to the user as "nothing found".
This is the one citation question that needs no judgement, so it is decided in
code and cannot depend on the model being careful.
"""

from __future__ import annotations

import pytest

import core.manager as manager

CHUNKS = [{"text": f"chunk {i}", "metadata": {"file_name": "sv-u.pdf"}} for i in range(9)]


@pytest.fixture
def llm_guard(monkeypatch):
    """Fail loudly if the compliance LLM is called — the short-circuit must
    decide before spending a ~13 s call on a question it can answer itself."""
    async def fail(*args, **kwargs):
        raise AssertionError("compliance LLM should not be reached")

    monkeypatch.setattr(manager, "call_llm", fail)


@pytest.mark.anyio
async def test_unsourced_answer_is_sent_back_for_rewrite(llm_guard):
    result = await manager.check_compliance(
        answer="Die Wendefahrt erfolgt in drei Phasen.",
        global_chunks=CHUNKS,
        use_case_prompt="p", use_case="wl", on_event=None,
    )

    assert result["verdict"] == "REWRITE"
    assert result["issues"]


@pytest.mark.anyio
async def test_guidance_names_the_valid_citation_range(llm_guard):
    """The rewrite pass gets this verbatim; without the range it can repeat the
    out-of-range numbering that produced the empty citation list."""
    result = await manager.check_compliance(
        answer="Ein Satz ohne Beleg.",
        global_chunks=CHUNKS,
        use_case_prompt="p", use_case="wl", on_event=None,
    )

    assert "[1]" in result["guidance"]
    assert f"[{len(CHUNKS)}]" in result["guidance"]


@pytest.mark.anyio
async def test_empty_pool_does_not_trigger_the_rewrite(llm_guard):
    """With nothing retrieved there is nothing to cite — demanding citations
    would loop the synthesizer against an impossible requirement."""
    result = await manager.check_compliance(
        answer="Dazu wurden keine Dokumente gefunden.",
        global_chunks=[],
        use_case_prompt="p", use_case="wl", on_event=None,
    )

    assert result["verdict"] == "OK"


@pytest.mark.anyio
async def test_a_cited_answer_reaches_the_llm_checker(monkeypatch):
    """The short-circuit must only catch the zero-citation case; everything
    else still needs the model's judgement on source authenticity."""
    seen: dict = {}

    async def fake_call_llm(messages, **kwargs):
        seen["called"] = True
        return '{"verdict": "OK", "issues": [], "guidance": ""}'

    monkeypatch.setattr(manager, "call_llm", fake_call_llm)

    result = await manager.check_compliance(
        answer="Die Wendefahrt erfolgt in drei Phasen [2].",
        global_chunks=CHUNKS,
        use_case_prompt="p", use_case="wl", on_event=None,
    )

    assert seen.get("called") is True
    assert result["verdict"] == "OK"


@pytest.mark.anyio
async def test_short_circuit_emits_a_done_event(llm_guard):
    """The UI drives its compliance badge off this event; skipping it would
    leave the phase stuck on "started"."""
    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    await manager.check_compliance(
        answer="Ohne Beleg.", global_chunks=CHUNKS,
        use_case_prompt="p", use_case="wl", on_event=on_event,
    )

    phases = [e.get("phase") for e in events if e.get("type") == "compliance"]
    assert phases == ["started", "done"]

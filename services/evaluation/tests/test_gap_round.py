"""Nachrecherche: eine zweite Runde nur bei einem benennbaren Loch.

Der Manager sieht nach dem Fan-out alle Fragmente und das Vokabular des
Bestands – ein einzelner Spezialist sieht immer nur seinen Ausschnitt. Deshalb
sitzt die informierte Zweitsuche hier und nicht im ReAct-Loop.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from core.manager import _find_gaps, _gap_vocabulary, propose_gap_subtasks


def _chunk(text, product_id=None):
    return {"text": text, "score": 0.9,
            "metadata": {"extra": {"product_id": product_id}}}


def _sub(sub_query, chunks=(), answer="…", error=None, sufficient=True):
    return {"subagent_id": "sub-1-facts", "role": "facts", "role_label": "Fakten",
            "sub_query": sub_query, "answer": answer, "chunks": list(chunks),
            "agent_steps": [], "sufficient": sufficient, "error": error}


# ── Lückensuche ──────────────────────────────────────────────


def test_answer_grounded_in_hits_is_no_gap():
    """Ein offener Aspekt ist kein Loch, solange Treffer vorliegen."""
    chunks = [_chunk("MESSERKOPF Ø63 mm", "11221.5277")]
    gaps = _find_gaps(
        "11221.5277 Dm63 Messerkopf nicht gefunden",
        use_case="gw_stpoelten",
        subagents=[_sub("Wo liegt der Messerkopf?", chunks)],
        global_chunks=chunks,
    )
    assert gaps == []


def test_hits_from_the_wrong_product_are_a_gap():
    """Der teuerste Fehler im Betrieb: Treffer, aber alle vom falschen Bauteil."""
    chunks = [_chunk("MESSERKOPF Ø63 mm", "11221.5805")]
    gaps = _find_gaps(
        "11221.5277 Neuer Mitarbeiter findet den Dm63 Messerkopf nicht",
        use_case="gw_stpoelten",
        subagents=[_sub("Wo liegt der Messerkopf?", chunks)],
        global_chunks=chunks,
    )
    assert len(gaps) == 1
    assert "11221.5277" in gaps[0]


def test_one_empty_facet_among_hits_is_no_gap():
    """Der Planer erfindet Facetten, die der Bestand nicht führt.

    Solange die anderen Spezialisten geliefert haben, ist das kein Loch —
    sonst läuft die Nachrunde bei fast jeder Anfrage.
    """
    gaps = _find_gaps(
        "Welches Werkzeug für Taschenfräsen?",
        use_case="gw_stpoelten",
        subagents=[_sub("Werkzeuge für Taschenfräsen", [_chunk("HB SF Ø10")]),
                   _sub("Gibt es technische Zeichnungen dazu?", [])],
        global_chunks=[_chunk("HB SF Ø10")],
    )
    assert gaps == []


def test_nothing_found_at_all_is_a_gap():
    gaps = _find_gaps(
        "Welches Werkzeug für Taschenfräsen?",
        use_case="gw_stpoelten",
        subagents=[_sub("Werkzeuge für Taschenfräsen", [])],
        global_chunks=[],
    )
    assert len(gaps) == 1
    assert "Werkzeuge für Taschenfräsen" in gaps[0]


def test_failed_subtask_is_a_gap():
    gaps = _find_gaps(
        "Welches Werkzeug?",
        use_case="gw_stpoelten",
        subagents=[_sub("Teilfrage", [], error="'dict' object has no attribute 'strip'")],
        global_chunks=[],
    )
    assert len(gaps) == 1 and "strip" in gaps[0]


# ── Vorschlag der neuen Teilfragen ───────────────────────────


def test_vocabulary_comes_from_the_corpus_not_the_user():
    vocab = _gap_vocabulary([_chunk("DM=63/45GRAD MESSERKOPF – PLAN SCHRUPPEN")])
    assert "DM=63/45GRAD" in vocab


@pytest.mark.anyio
async def test_proposal_drops_repeats_and_caps_at_two():
    llm = AsyncMock(return_value=json.dumps({"subtasks": [
        {"sub_query": "Wo liegt der Messerkopf?", "focus": "Wiederholung"},
        {"sub_query": "Einstellblatt 11221.5277 Werkzeugplätze Tr.50", "focus": "Plätze"},
        {"sub_query": "DM=63/45GRAD MESSERKOPF Schnittwerte", "focus": "Werte"},
        {"sub_query": "Noch eine vierte Frage zum Messerkopf", "focus": "zu viel"},
    ]}))
    with patch("core.manager.call_llm", llm):
        subtasks = await propose_gap_subtasks(
            "11221.5277 Dm63 Messerkopf nicht gefunden",
            use_case="gw_stpoelten",
            gaps=["Zu Bauteil 11221.5277 liegt kein Treffer vor."],
            subagents=[_sub("Wo liegt der Messerkopf?")],
            global_chunks=[_chunk("DM=63/45GRAD MESSERKOPF")],
        )

    assert [st["sub_query"] for st in subtasks] == [
        "Einstellblatt 11221.5277 Werkzeugplätze Tr.50",
        "DM=63/45GRAD MESSERKOPF Schnittwerte",
    ]
    assert all(st["role"] == "facts" for st in subtasks)


@pytest.mark.anyio
async def test_no_promising_query_means_no_second_round():
    """Eine leere Liste ist die richtige Antwort, wenn nichts mehr zu holen ist."""
    with patch("core.manager.call_llm", AsyncMock(return_value='{"subtasks": []}')):
        subtasks = await propose_gap_subtasks(
            "Frage", use_case="gw_stpoelten", gaps=["Loch"],
            subagents=[_sub("Teilfrage")], global_chunks=[],
        )
    assert subtasks == []


@pytest.mark.anyio
async def test_planner_failure_is_not_fatal():
    with patch("core.manager.call_llm", AsyncMock(side_effect=RuntimeError("LLM weg"))):
        subtasks = await propose_gap_subtasks(
            "Frage", use_case="gw_stpoelten", gaps=["Loch"],
            subagents=[_sub("Teilfrage")], global_chunks=[],
        )
    assert subtasks == []


# ── Verdrahtung: Deckelung der Runde ─────────────────────────


async def _run_round(subagents, elapsed_s=1.0):
    from core.manager import _run_gap_round

    return await _run_gap_round(
        "11221.5277 Neuer Mitarbeiter findet den Dm63 Messerkopf nicht",
        use_case="gw_stpoelten",
        session_id="test-session",
        use_case_prompt="Du bist ein CNC-Rüstexperte.",
        available_actions=["SEARCH", "SEARCH_CNC", "FINAL_ANSWER"],
        collection="gw_cnc_steps",
        filters={},
        images=None,
        image_context="",
        subagents=subagents,
        elapsed_s=elapsed_s,
        on_event=None,
        request_id=None,
        username=None,
    )


@pytest.mark.anyio
async def test_late_answer_beats_no_answer_round_is_skipped():
    """Ist die Zeit fast verbraucht, fällt die Runde aus – ohne LLM-Aufruf."""
    proposer = AsyncMock()
    with patch("core.manager.propose_gap_subtasks", proposer):
        extra, phase = await _run_round([_sub("Teilfrage", [])], elapsed_s=999.0)

    assert (extra, phase) == ([], None)
    proposer.assert_not_awaited()


@pytest.mark.anyio
async def test_gap_round_runs_on_a_tight_step_budget():
    proposer = AsyncMock(return_value=[
        {"role": "facts", "sub_query": "Einstellblatt 11221.5277 Werkzeugplätze",
         "focus": "Plätze"},
    ])
    runner = AsyncMock(return_value=_sub("Einstellblatt 11221.5277 Werkzeugplätze",
                                         [_chunk("Tr.50-Nr.19-20", "11221.5277")]))
    with patch("core.manager.propose_gap_subtasks", proposer), \
            patch("core.manager._run_one_subagent", runner):
        extra, phase = await _run_round([_sub("Wo liegt der Messerkopf?", [])])

    assert len(extra) == 1
    assert phase["id"] == "gap_round" and phase["label"] == "Nachrecherche"
    assert runner.await_args.kwargs["role"].max_steps == 2
    # Die Nummerierung läuft hinter Runde 1 weiter, damit das Frontend die
    # Ereignisse nicht der falschen Gruppe zuordnet.
    assert runner.await_args.kwargs["sub_id"] == "sub-2-facts"


@pytest.mark.anyio
async def test_no_gap_no_round():
    chunks = [_chunk("MESSERKOPF Ø63 mm", "11221.5277")]
    proposer = AsyncMock()
    with patch("core.manager.propose_gap_subtasks", proposer):
        extra, phase = await _run_round([_sub("Wo liegt der Messerkopf?", chunks)])

    assert (extra, phase) == ([], None)
    proposer.assert_not_awaited()


# ── Unbrauchbare Vorschläge ──────────────────────────────────


@pytest.mark.anyio
async def test_pseudo_syntax_proposals_are_discarded():
    """Im Betrieb wörtlich zurückgekommen: ein Aufruf statt einer Frage.

    Als Suchtext geht so etwas in die Einbettung und trifft nichts.
    """
    llm = AsyncMock(return_value=json.dumps({"subtasks": [
        {"sub_query": "SEARCH_CNC(product_family=11221.5*) | grep -i 'Ø10'", "focus": "x"},
        {"sub_query": "SEARCH_DOCUMENTATION(article=11221.5805)", "focus": "x"},
        {"sub_query": "Welche VHM-Fräser mit 10 mm nutzt Bauteil 11221.5805?", "focus": "ok"},
    ]}))
    with patch("core.manager.call_llm", llm):
        subtasks = await propose_gap_subtasks(
            "11221.5805 Ø10 VHM Fräser fehlt",
            use_case="gw_stpoelten", gaps=["Loch"],
            subagents=[_sub("Teilfrage")], global_chunks=[],
        )

    assert [st["sub_query"] for st in subtasks] == [
        "Welche VHM-Fräser mit 10 mm nutzt Bauteil 11221.5805?"
    ]


@pytest.mark.anyio
async def test_markdown_is_stripped_not_rejected():
    llm = AsyncMock(return_value=json.dumps({"subtasks": [
        {"sub_query": "Welche **Werkzeuge** nutzt Bauteil 11221.5277?", "focus": "x"},
    ]}))
    with patch("core.manager.call_llm", llm):
        subtasks = await propose_gap_subtasks(
            "Frage", use_case="gw_stpoelten", gaps=["Loch"],
            subagents=[_sub("Teilfrage")], global_chunks=[],
        )
    assert subtasks[0]["sub_query"] == "Welche Werkzeuge nutzt Bauteil 11221.5277?"


@pytest.mark.anyio
async def test_a_slow_round_is_dropped_rather_than_doubling_the_request():
    import asyncio

    from core.manager import settings

    async def _never(*a, **kw):
        await asyncio.sleep(30)

    with patch.object(settings, "GAP_ROUND_TIMEOUT_S", 0.05), \
            patch("core.manager.propose_gap_subtasks", _never):
        extra, phase = await _run_round([_sub("Teilfrage", [])])

    assert (extra, phase) == ([], None)

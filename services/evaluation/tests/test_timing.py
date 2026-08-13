"""Pipeline timing report used to surface bottlenecks in the UI."""

from core.timing import (
    step_timing_children,
    subagent_phase_children,
    timing_report,
)


def test_timing_report_picks_slowest_phase():
    report = timing_report(
        [
            {"id": "plan", "label": "Planung", "ms": 8000},
            {"id": "subagents", "label": "Sub-Agents", "ms": 3000},
            {"id": "synthesize", "label": "Synthese", "ms": 1200},
        ],
        total_ms=12200,
    )
    assert report["bottleneck"]["id"] == "plan"
    assert report["bottleneck"]["share_pct"] == 66
    assert report["total_ms"] == 12200


def test_timing_report_empty():
    report = timing_report([], total_ms=0)
    assert report["bottleneck"] is None
    assert report["phases"] == []


def test_timing_report_drills_into_dominant_subagent():
    report = timing_report(
        [
            {"id": "plan", "label": "Planung", "ms": 8100},
            {
                "id": "subagents",
                "label": "Sub-Agents (parallel)",
                "ms": 300200,
                "children": [
                    {
                        "id": "sub-1-facts",
                        "label": "Fakten",
                        "ms": 300200,
                        "children": [
                            {"id": "step-1-SEARCH", "label": "1 SEARCH", "ms": 52000},
                            {"id": "step-2-SEARCH", "label": "2 SEARCH", "ms": 61000},
                            {"id": "step-3-FINAL_ANSWER", "label": "3 FINAL_ANSWER", "ms": 180000},
                        ],
                    },
                    {"id": "sub-2-context", "label": "Kontext", "ms": 86100},
                ],
            },
            {"id": "synthesize", "label": "Synthese", "ms": 10900},
        ],
        total_ms=330400,
    )
    neck = report["bottleneck"]
    assert neck["id"] == "step-3-FINAL_ANSWER"
    assert neck["label"] == "3 FINAL_ANSWER"
    assert neck["share_pct"] == 54


def test_timing_report_stays_at_subagent_when_steps_are_even():
    report = timing_report(
        [
            {
                "id": "subagents",
                "label": "Sub-Agents (parallel)",
                "ms": 300200,
                "children": [
                    {
                        "id": "sub-1-facts",
                        "label": "Fakten",
                        "ms": 300200,
                        "children": [
                            {"id": "step-1-SEARCH", "label": "1 SEARCH", "ms": 100000},
                            {"id": "step-2-SEARCH", "label": "2 SEARCH", "ms": 100000},
                            {"id": "step-3-FINAL_ANSWER", "label": "3 FINAL_ANSWER", "ms": 100000},
                        ],
                    },
                    {"id": "sub-2-context", "label": "Kontext", "ms": 86100},
                ],
            },
        ],
        total_ms=330400,
    )
    assert report["bottleneck"]["id"] == "sub-1-facts"
    assert report["bottleneck"]["label"] == "Fakten"


def test_step_timing_children_nests_retrieval():
    children = step_timing_children([
        {
            "step": 1,
            "action": "SEARCH",
            "duration_ms": 52000,
            "llm_ms": 48000,
            "embed_ms": 2100,
            "search_ms": 800,
            "rerank_ms": 400,
        }
    ])
    assert children[0]["label"] == "1 SEARCH"
    assert children[0]["llm_ms"] == 48000
    assert [p["label"] for p in children[0]["children"]] == ["Embed", "Suche", "Rerank"]


def test_subagent_phase_children_sums_llm():
    children = subagent_phase_children([
        {
            "subagent_id": "sub-1-facts",
            "role_label": "Fakten",
            "duration_ms": 300200,
            "agent_steps": [
                {"step": 1, "action": "SEARCH", "duration_ms": 100000, "llm_ms": 90000},
                {"step": 2, "action": "FINAL_ANSWER", "duration_ms": 200000, "llm_ms": 198000},
            ],
        }
    ])
    assert children[0]["llm_ms"] == 288000
    assert len(children[0]["children"]) == 2

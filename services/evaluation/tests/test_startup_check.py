"""Tests for the startup self-test aggregation (all probes mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import core.startup_check as sc
import pytest


def _ok(component: str) -> dict:
    return {"component": component, "name": "m", "use_cases": ["*"], "ok": True, "message": ""}


@pytest.mark.anyio
async def test_all_ok_reports_ok():
    with patch.object(sc, "_use_case_ids", AsyncMock(return_value=[])), \
         patch.object(sc, "_check_llms", AsyncMock(return_value=[_ok("llm.chat")])), \
         patch.object(sc, "_check_embedding", AsyncMock(return_value=[_ok("embedding")])), \
         patch.object(sc, "_check_reranker", AsyncMock(return_value=_ok("reranker"))):
        result = await sc.run_checks()
    assert result["ok"] is True
    assert [c["component"] for c in result["checks"]] == ["llm.chat", "embedding", "reranker"]
    assert sc.last_result() is result


@pytest.mark.anyio
async def test_one_failure_fails_the_whole_check():
    broken = {**_ok("reranker"), "ok": False, "message": "Connection refused"}
    with patch.object(sc, "_use_case_ids", AsyncMock(return_value=[])), \
         patch.object(sc, "_check_llms", AsyncMock(return_value=[_ok("llm.chat")])), \
         patch.object(sc, "_check_embedding", AsyncMock(return_value=[_ok("embedding")])), \
         patch.object(sc, "_check_reranker", AsyncMock(return_value=broken)):
        result = await sc.run_checks()
    assert result["ok"] is False


@pytest.mark.anyio
async def test_disabled_reranker_is_skipped_not_failed(monkeypatch):
    monkeypatch.setattr(sc, "reranker_status", lambda: {"enabled": False, "model": "bge"})
    check = await sc._check_reranker()
    assert check["ok"] is True and check["skipped"] is True


def test_reranker_probe_detects_indifferent_model(monkeypatch):
    """A model that scores relevant and irrelevant alike must fail the probe –
    that is the silent misconfiguration crashes never reveal."""
    import core.reranker as reranker_mod

    class Indifferent:
        def predict(self, pairs, show_progress_bar=False):
            return [0.5 for _ in pairs]

    monkeypatch.setattr(reranker_mod, "_RERANKER_ENABLED", True)
    monkeypatch.setattr(reranker_mod, "_model", Indifferent())
    probe = reranker_mod.probe_reranker()
    assert probe["ok"] is False
    assert "unterscheidet nicht" in probe["message"]


def test_reranker_probe_rejects_scores_without_a_clear_cut(monkeypatch):
    """Ranked correctly but both "relevant" – what vLLM without the Qwen3
    template returned. The labels downstream would be meaningless."""
    import core.reranker as reranker_mod

    class Blurry:
        def predict(self, pairs, show_progress_bar=False):
            return [2.45, 0.15]

    monkeypatch.setattr(reranker_mod, "_RERANKER_ENABLED", True)
    monkeypatch.setattr(reranker_mod, "_model", Blurry())
    assert reranker_mod.probe_reranker()["ok"] is False

"""Tests für action_search_cnc – Werkzeugempfehlung (GW St. Pölten).

Embedding- und VectorDB-Aufrufe werden über einen gefälschten httpx-Client
gemockt, damit die Aggregations-/Ranking-Logik isoliert geprüft wird.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.actions import action_search_cnc, _is_tool_material, _parse_diameter


def test_parse_diameter_prefers_real_size_over_cutter_count():
    assert _parse_diameter("10 mm Durchmesser, 3 Schneiden") == 10.0
    assert _parse_diameter("VHM-Fräser 3 Schneiden", "Ø10") == 10.0
    assert _parse_diameter("Taschenfräsen") is None
    # bloße Zahl ohne Marker, aber keine Schneidenzahl
    assert _parse_diameter("12 VHM-Fräser") == 12.0
    # nur Schneidenzahl -> kein Durchmesser
    assert _parse_diameter("Fräser mit 3 Schneiden") is None


def test_is_tool_material():
    assert _is_tool_material("VHM")
    assert _is_tool_material("HSS")
    assert not _is_tool_material("EN AW-6005A T6")
    assert not _is_tool_material("Aluminium")


def _cnc_hit(*, tool_type, diameter, product_id, operation_type,
             spindle_speed=None, feed=None, material_class=None, score=0.9):
    """Ein VectorDB-Suchtreffer, wie ihn /v1/search liefert (extra nested)."""
    return {
        "chunk_id": f"{product_id}-{tool_type}-{diameter}",
        "score": score,
        "text": "…",
        "metadata": {
            "collection": "gw_cnc_steps",
            "extra": {
                "tool_type": tool_type,
                "diameter": diameter,
                "product_id": product_id,
                "operation_type": operation_type,
                "spindle_speed": spindle_speed,
                "feed": feed,
                "material_class": material_class,
            },
        },
    }


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeClient:
    """Mockt embed (/v1/embed) + search (/v1/search). ``search_by_filter``
    bildet die filters-Signatur → Trefferliste ab, um den Material-Filter-
    Fallback testen zu können."""

    def __init__(self, search_by_filter):
        self._search_by_filter = search_by_filter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        if url.endswith("/v1/embed"):
            return _FakeResp({"vector": [0.1, 0.2, 0.3], "model": "bge-m3"})
        if url.endswith("/v1/search"):
            has_material = "extra.material_class" in (json.get("filters") or {})
            return _FakeResp({"results": self._search_by_filter(has_material)})
        raise AssertionError(f"unexpected url {url}")


def _patch_client(search_by_filter):
    return patch("core.actions.httpx.AsyncClient",
                 lambda *a, **k: _FakeClient(search_by_filter))


@pytest.mark.anyio
async def test_recommends_most_used_tool():
    hits = [
        _cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7126",
                 operation_type="Taschenfräsen", spindle_speed=8200, feed=3000,
                 material_class="EN AW-6005A T6"),
        _cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7238",
                 operation_type="Taschenfräsen", spindle_speed=8000, feed=3000,
                 material_class="EN AW-6005A T6"),
        _cnc_hit(tool_type="VHM-FRAESER", diameter=12.0, product_id="11221.7199",
                 operation_type="Taschenfräsen", score=0.7),
    ]
    with _patch_client(lambda has_mat: hits):
        res = await action_search_cnc(
            {"operation": "Taschenfräsen", "material": "EN AW-6005A T6"},
            use_case="gw_stpoelten",
        )
    obs = res["observation"]
    # HB SF Ø10 in 2 Bauteilen -> Top-Empfehlung
    assert "HB SF Ø10 mm" in obs
    assert "in 2 Bauteil" in obs
    assert "Empfehlung: HB SF Ø10 mm" in obs
    # Parameterbereich aggregiert
    assert "S 8000–8200" in obs
    assert res["searched_collections"] == ["gw_cnc_steps"]
    # Citations: zwei aggregierte Werkzeuge
    assert len(res["chunks"]) == 2


@pytest.mark.anyio
async def test_missing_tool_excluded_for_alternative():
    hits = [
        _cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7126",
                 operation_type="Taschenfräsen"),
        _cnc_hit(tool_type="VHM-FRAESER", diameter=12.0, product_id="11221.7199",
                 operation_type="Taschenfräsen"),
    ]
    with _patch_client(lambda has_mat: hits):
        res = await action_search_cnc(
            {"operation": "Taschenfräsen", "missing_tool": "HB SF"},
            use_case="gw_stpoelten",
        )
    obs = res["observation"]
    assert "HB SF" not in obs.replace("ohne: HB SF", "")  # nur im Header
    assert "VHM-FRAESER Ø12 mm" in obs


@pytest.mark.anyio
async def test_material_filter_fallback_when_no_exact_match():
    only_unfiltered = [
        _cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7126",
                 operation_type="Taschenfräsen"),
    ]
    # Mit Material-Filter: leer; ohne Filter: Treffer -> Fallback greift
    def by_filter(has_material):
        return [] if has_material else only_unfiltered

    with _patch_client(by_filter):
        res = await action_search_cnc(
            {"operation": "Taschenfräsen", "material": "Aluminium"},
            use_case="gw_stpoelten",
        )
    assert "kein exakter Material-Treffer" in res["observation"]
    assert "HB SF Ø10 mm" in res["observation"]


@pytest.mark.anyio
async def test_no_results():
    with _patch_client(lambda has_mat: []):
        res = await action_search_cnc(
            {"operation": "Gewindebohren"}, use_case="gw_stpoelten"
        )
    assert "Keine passenden" in res["observation"]
    assert res["chunks"] == []


@pytest.mark.anyio
async def test_use_case_isolation_blocks_non_gw():
    res = await action_search_cnc({"operation": "Bohren"}, use_case="neumann")
    assert "nicht verfügbar" in res["observation"]
    assert res["chunks"] == []


@pytest.mark.anyio
async def test_diameter_constraint_filters_other_sizes():
    """Frage nach Ø10 darf nicht von Ø9-Bohrern / Ø16-Anbohrern verwässert werden."""
    hits = [
        _cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7126",
                 operation_type="Taschenfräsen"),
        _cnc_hit(tool_type="VHMI-BOHRER", diameter=9.0, product_id="11221.7126",
                 operation_type="Bohren"),
        _cnc_hit(tool_type="VHM-NC-ANBOHRER", diameter=16.0, product_id="11221.7239",
                 operation_type="Anbohren"),
    ]
    with _patch_client(lambda has_mat: hits):
        res = await action_search_cnc(
            {"operation": "Fräsen", "missing_tool": "10 mm VHM-Fräser 3 Schneiden"},
            use_case="gw_stpoelten",
        )
    obs = res["observation"]
    assert "Ø 10 mm" in obs
    assert "HB SF Ø10 mm" in obs
    # andere Durchmesser sind raus
    assert "Ø9" not in obs and "Ø16" not in obs
    assert "VHMI-BOHRER" not in obs and "ANBOHRER" not in obs


@pytest.mark.anyio
async def test_tool_material_not_treated_as_workpiece():
    """material='VHM' (Schneidstoff) darf nicht als Werkstück-Material gelten."""
    captured = {}

    class _CaptureClient(_FakeClient):
        async def post(self, url, json=None, **kw):
            if url.endswith("/v1/search"):
                captured["filters"] = json.get("filters")
            return await super().post(url, json=json, **kw)

    hits = [_cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7126",
                     operation_type="Taschenfräsen")]
    with patch("core.actions.httpx.AsyncClient",
               lambda *a, **k: _CaptureClient(lambda has_mat: hits)):
        res = await action_search_cnc(
            {"operation": "Taschenfräsen", "material": "VHM"},
            use_case="gw_stpoelten",
        )
    # kein Material-Filter gesetzt (VHM ist Schneidstoff, kein Werkstück)
    assert not captured.get("filters")
    assert "Material: VHM" not in res["observation"]
    assert "HB SF Ø10 mm" in res["observation"]

"""Use Cases im Dashboard anlegen: die Collection muss durchsuchbar sein."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from main import app
from router.admin import collection_outside_prefixes


def test_collection_inside_explicit_prefix_is_fine():
    assert collection_outside_prefixes("kunde_x", "kx_handbuecher", ["kx_"]) is None


def test_without_prefixes_the_id_is_the_prefix():
    assert collection_outside_prefixes("kunde_x", "kunde_x_default", []) is None
    assert collection_outside_prefixes("kunde_x", "handbuecher", []) is not None


def test_mismatch_explains_why_it_matters():
    problem = collection_outside_prefixes("kunde_x", "docs", ["kx_"])
    assert "docs" in problem and "kx_" in problem
    assert "nie gefunden" in problem


def test_shipped_use_cases_pass():
    import json
    from pathlib import Path

    cases = json.loads((Path(__file__).parents[1] / "config" / "use_cases.json").read_text())
    cases = cases if isinstance(cases, list) else cases.get("use_cases", [])
    for uc in cases:
        assert collection_outside_prefixes(
            uc["id"], uc["default_collection"], uc.get("collection_prefixes") or []
        ) is None, uc["id"]


@pytest.mark.anyio
async def test_saving_an_unsearchable_collection_is_refused():
    upsert = AsyncMock()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("router.admin.upsert_use_case", new=upsert):
            resp = await client.put("/v1/admin/use-cases/kunde_x", json={
                "slug": "kunde-x", "label": "Kunde X",
                "default_collection": "docs", "collection_prefixes": ["kx_"],
            })
    assert resp.status_code == 400
    assert "nie gefunden" in resp.json()["detail"]
    upsert.assert_not_awaited()

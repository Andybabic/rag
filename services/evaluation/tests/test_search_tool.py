"""SEARCH_TOOL – Werkzeug im Lagerbestand nachschlagen.

Dritte Quelle neben Programm und Einstellblatt: Das Programm sagt, womit
gefertigt wurde, das Einstellblatt, wo das Werkzeug liegt, diese Liste, ob
eines da ist.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.actions import _tool_row_line, action_search_tool

_ROW = (
    "Artikelnummer: 1008400186\n"
    "Bezeichnung 1: VHM Spiralbohrer\n"
    "Bezeichnung 2: DM 4,65 mm\n"
    "PPS-ID: 70230.1234\n"
    "Kaestchenklasse: 12055\n"
    "Bestand: 2.00\n"
    "min Bestand: 1\n"
    "Name Hauptlieferant: Hommel Hercules-Werkzeughandel GmbH"
)


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, results, search_status=200):
        self._results = results
        self._status = search_status
        self.searched: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        if url.endswith("/v1/embed"):
            return _Resp({"vector": [0.1, 0.2], "model": "bge-m3"})
        if url.endswith("/v1/search"):
            self.searched.append(json)
            if self._status != 200:
                return _Resp({}, status_code=self._status)
            return _Resp({"results": self._results})
        raise AssertionError(f"unexpected url {url}")


def _patch(client):
    return patch("core.actions.httpx.AsyncClient", lambda *a, **k: client)


def _hit(text=_ROW, score=0.9):
    return {"chunk_id": "r1", "score": score, "text": text,
            "metadata": {"file_name": "Werkzeugliste_Ecomat_Hauptlieferant.csv",
                         "row_index": 7, "table_key": "werkzeugliste_ecomat_hauptlieferant"}}


def test_row_becomes_one_readable_line():
    line = _tool_row_line(_ROW)
    assert "Artikel 1008400186" in line
    # Der Stichtag hängt an der Zahl, damit er die Synthese überlebt.
    assert "Bestand 2.00 (Stand des Listen-Imports)" in line
    assert "Hommel" in line
    # Interne Kennzahl gehört nicht in eine Rüstauskunft.
    assert "12055" not in line


@pytest.mark.anyio
async def test_lookup_returns_stock_and_supplier():
    client = _Client([_hit()])
    with _patch(client):
        res = await action_search_tool({"tool": "VHM-Bohrer 4,65"},
                                       use_case="gw_stpoelten")

    obs = res["observation"]
    assert "Artikel 1008400186" in obs and "Bestand 2.00" in obs
    assert client.searched[0]["collection"] == "gw_stpoelten_default"
    assert res["chunks"][0]["metadata"]["file_name"].endswith(".csv")


@pytest.mark.anyio
async def test_stock_is_labelled_as_a_snapshot():
    """Sonst verspricht die Antwort zwei Stück, die im Regal längst fehlen."""
    with _patch(_Client([_hit()])):
        res = await action_search_tool({"tool": "Gewindeformer M6"},
                                       use_case="gw_stpoelten")

    assert "keine Live-Abfrage" in res["observation"]


@pytest.mark.anyio
async def test_missing_list_is_a_missing_source_not_an_error():
    """Ohne Import gibt es die Collection nicht – das ist kein Fehler."""
    with _patch(_Client([], search_status=404)):
        res = await action_search_tool({"tool": "VHM-Bohrer"},
                                       use_case="gw_stpoelten")

    assert "Keine Werkzeugliste importiert" in res["observation"]
    assert res["chunks"] == []


@pytest.mark.anyio
async def test_without_a_tool_name_it_asks_for_one():
    with _patch(_Client([_hit()])):
        res = await action_search_tool({}, use_case="gw_stpoelten")
    assert "benötigt eine Werkzeugbezeichnung" in res["observation"]


@pytest.mark.anyio
async def test_other_use_cases_cannot_reach_the_list():
    with _patch(_Client([_hit()])):
        res = await action_search_tool({"tool": "Bohrer"}, use_case="neumann")
    assert "nicht verfügbar" in res["observation"]
    assert res["searched_collections"] == []


@pytest.mark.anyio
async def test_a_weak_match_is_not_sold_as_the_tool():
    """Die Vektorsuche liefert immer etwas.

    Gemessen: Der Dm63 Messerkopf steht nicht in der Liste und erreicht 0,58 –
    zurück kam ein Ø6-Fräser. Ohne Untergrenze wird daraus eine Auskunft.
    """
    fraeser = _hit(
        text="Artikelnummer: 1002166342\nBezeichnung 1: HACHENBACH HPC ALU STAR\n"
             "Bezeichnung 2: FRÄSER DM 6,00\nBestand: 5.00",
        score=0.58,
    )
    with _patch(_Client([fraeser])):
        res = await action_search_tool({"tool": "Dm63 Messerkopf"},
                                       use_case="gw_stpoelten")

    obs = res["observation"]
    assert "steht NICHT in der Werkzeugliste" in obs
    assert "0.58" in obs
    assert "keine Live-Abfrage" not in obs  # kein Bestand, der zu nennen wäre
    assert "FRÄSER DM 6,00" in obs  # sichtbar, aber als bloss ähnlich markiert

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
        if url.endswith("/v1/scroll"):
            # Rüstunterlagen werden vollständig gelesen, nicht gesucht.
            return _FakeResp({"points": []})
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


@pytest.mark.anyio
async def test_fixture_programs_do_not_count_as_parts_and_machines_are_named():
    """Vorrichtungsprogramme fertigen die Spannvorrichtung, nicht das Bauteil.

    Ihre Werkzeuge dürfen „in N Bauteilen verwendet" nicht aufblähen – sonst
    gewinnt ein Werkzeug die Empfehlung, das am Bauteil nie lief.
    """
    fixture_hits = [
        _cnc_hit(tool_type="VHM-FRAESER", diameter=16.0, product_id=pid,
                 operation_type="Fräsen", score=0.95)
        for pid in ("11221.7500", "11221.6102", "11221.3308")
    ]
    for hit in fixture_hits:
        hit["metadata"]["extra"]["program_role"] = "vorrichtung"
    part_hits = [
        _cnc_hit(tool_type="HB SF", diameter=16.0, product_id="11221.7199",
                 operation_type="Fräsen", score=0.8),
    ]
    part_hits[0]["metadata"]["extra"]["machine"] = "Chiron 243"

    with _patch_client(lambda has_mat: fixture_hits + part_hits):
        res = await action_search_cnc({"operation": "Fräsen"}, use_case="gw_stpoelten")

    obs = res["observation"]
    assert "Empfehlung: HB SF Ø16 mm" in obs
    assert "VHM-FRAESER" not in obs
    assert "Maschine(n) Chiron 243" in obs
    assert res["chunks"][0]["metadata"]["machines"] == ["Chiron 243"]


class _FilterAwareClient(_FakeClient):
    """Wie ``_FakeClient``, reicht der Testfunktion aber die kompletten
    ``filters`` durch – für die Bauteil-Eingrenzung."""

    async def post(self, url, json=None, **kw):
        if url.endswith("/v1/embed"):
            return _FakeResp({"vector": [0.1, 0.2, 0.3], "model": "bge-m3"})
        if url.endswith("/v1/search"):
            return _FakeResp({"results": self._search_by_filter(json.get("filters") or {})})
        if url.endswith("/v1/scroll"):
            return _FakeResp({"points": []})
        raise AssertionError(f"unexpected url {url}")


def _patch_client_filters(search_by_filter):
    return patch("core.actions.httpx.AsyncClient",
                 lambda *a, **k: _FilterAwareClient(search_by_filter))


@pytest.mark.anyio
async def test_product_id_scopes_the_search():
    """Die Bauteilnummer grenzt die Suche ein – auch wenn das Modell sie in
    ``operation`` steckt, statt sie als ``product_id`` zu übergeben."""
    own = [_cnc_hit(tool_type="MESSERKOPF", diameter=63.0, product_id="11221.5277",
                    operation_type="Planfräsen")]
    foreign = [_cnc_hit(tool_type="VHMI-BOHRER", diameter=5.0, product_id="11221.5805",
                        operation_type="Bohren")]
    seen: list[dict] = []

    def by_filter(filters):
        seen.append(filters)
        return own if filters.get("extra.product_id") == "11221.5277" else foreign

    with _patch_client_filters(by_filter):
        res = await action_search_cnc(
            {"operation": "11221.5277", "material": ""}, use_case="gw_stpoelten"
        )

    assert seen[0] == {"extra.product_id": "11221.5277"}
    obs = res["observation"]
    assert "Bauteil: 11221.5277" in obs
    assert "MESSERKOPF Ø63 mm" in obs
    assert "VHMI-BOHRER" not in obs  # fremdes Bauteil
    assert "ACHTUNG" not in obs


@pytest.mark.anyio
async def test_foreign_products_are_flagged_and_not_recommended():
    """Ohne Daten zum gefragten Bauteil sind fremde Treffer keine Empfehlung."""
    foreign = [_cnc_hit(tool_type="MESSERKOPF", diameter=63.0, product_id="11221.5805",
                        operation_type="Planfräsen")]

    def by_filter(filters):
        return [] if filters.get("extra.product_id") else foreign

    with _patch_client_filters(by_filter):
        res = await action_search_cnc(
            {"operation": "Planfräsen", "product_id": "11221.5277"},
            use_case="gw_stpoelten",
        )

    obs = res["observation"]
    assert "ACHTUNG" in obs and "11221.5277" in obs
    assert "Empfehlung:" not in obs


@pytest.mark.anyio
async def test_chunks_without_tool_type_are_not_recommended():
    hits = [
        _cnc_hit(tool_type=None, diameter=None, product_id="11221.7126",
                 operation_type="Bohren"),
        _cnc_hit(tool_type=None, diameter=None, product_id="11221.7238",
                 operation_type="Planfräsen"),
        _cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7199",
                 operation_type="Taschenfräsen"),
    ]
    with _patch_client(lambda has_mat: hits):
        res = await action_search_cnc({"operation": "Taschenfräsen"},
                                      use_case="gw_stpoelten")

    obs = res["observation"]
    assert "unbekanntes Werkzeug" not in obs
    assert "Empfehlung: HB SF Ø10 mm" in obs


@pytest.mark.anyio
async def test_object_args_do_not_crash():
    """Kleine Modelle liefern für ein Feld auch mal ein Objekt statt Text."""
    hits = [_cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.7126",
                     operation_type="Taschenfräsen")]
    with _patch_client(lambda has_mat: hits):
        res = await action_search_cnc(
            {"operation": {"value": "Taschenfräsen"},
             "material": {"bauteil": "11221.7126"},
             "missing_tool": ["VHM", "Fräser"]},
            use_case="gw_stpoelten",
        )

    assert "HB SF Ø10 mm" in res["observation"]


# ── Werkzeugnummer aus dem Programm ("T5") ───────────────────


def _cnc_hit_tid(tool_id, **kw):
    hit = _cnc_hit(**kw)
    hit["metadata"]["extra"]["tool_id"] = tool_id
    return hit


@pytest.mark.anyio
async def test_tool_number_is_filtered_and_shown():
    """„Bei 11221.3350 fehlen zu T5 die Schnittdaten" – T5 steht als
    ``extra.tool_id`` am Schritt, samt Drehzahl und Vorschub."""
    t5 = [_cnc_hit_tid("T5", tool_type="FASENFRAESER", diameter=8.0,
                       product_id="11221.3350", operation_type="Fasenfräsen",
                       spindle_speed=12000, feed=4000)]
    seen: list[dict] = []

    def by_filter(filters):
        seen.append(filters)
        return t5 if filters.get("extra.tool_id") == "T5" else []

    with _patch_client_filters(by_filter):
        res = await action_search_cnc(
            {"operation": "T5 Schnittdaten", "product_id": "11221.3350"},
            use_case="gw_stpoelten",
        )

    assert seen[0] == {"extra.tool_id": "T5", "extra.product_id": "11221.3350"}
    obs = res["observation"]
    assert "Werkzeug-Nr. T5" in obs
    assert "S 12000" in obs and "F 4000" in obs
    assert "ACHTUNG" not in obs


@pytest.mark.anyio
async def test_unknown_tool_number_says_so():
    other = [_cnc_hit_tid("T12", tool_type="MESSERKOPF", diameter=125.0,
                          product_id="11221.3350", operation_type="Planfräsen")]

    def by_filter(filters):
        return [] if filters.get("extra.tool_id") else other

    with _patch_client_filters(by_filter):
        res = await action_search_cnc(
            {"tool_id": "T99", "product_id": "11221.3350"},
            use_case="gw_stpoelten",
        )

    obs = res["observation"]
    assert "T99" in obs and "ACHTUNG" in obs
    assert "Werkzeug-Nr. T12" in obs


@pytest.mark.anyio
async def test_the_product_filter_outlives_the_tool_number():
    """Werkzeugnummern gelten je Programm: „T5" in einem anderen Bauteil ist
    ein anderes Werkzeug. Deshalb fällt die Nummer vor dem Bauteil."""
    own = [_cnc_hit_tid("T12", tool_type="MESSERKOPF", diameter=125.0,
                        product_id="11221.3350", operation_type="Planfräsen")]
    seen: list[dict] = []

    def by_filter(filters):
        seen.append(dict(filters))
        if filters.get("extra.tool_id"):
            return []
        return own if filters.get("extra.product_id") else []

    with _patch_client_filters(by_filter):
        res = await action_search_cnc(
            {"tool_id": "T5", "product_id": "11221.3350"},
            use_case="gw_stpoelten",
        )

    # Nie nach der Werkzeugnummer ohne Bauteil gesucht.
    assert not any("extra.tool_id" in f and "extra.product_id" not in f for f in seen)
    assert "11221.3350" in res["observation"]


def test_material_state_is_not_a_tool_number():
    """„EN AW-6005A T6" ist ein Werkstoffzustand, keine Werkzeugnummer.

    Gleiches Muster, gegenteilige Bedeutung – und der Werkstoff kommt in
    diesem Bestand bei jedem zweiten Bauteil vor.
    """
    from core.actions import _TOOL_ID_RE, _without_material_tempers

    assert not _TOOL_ID_RE.findall(_without_material_tempers("EN AW-6005A T6"))
    assert not _TOOL_ID_RE.findall(_without_material_tempers("6082 T651"))
    assert _TOOL_ID_RE.findall(_without_material_tempers("Werkzeug T5 fehlt")) == ["5"]


def test_material_state_survives_tool_number_stripping():
    """Der Werkstoff darf beim Herauslösen der Nummer nicht verschwinden."""
    from core.actions import _strip_tool_ids

    assert "AW-6005A T6" in _strip_tool_ids("Fräsen EN AW-6005A T6 mit Werkzeug T5")


# ── Nachprüfbarkeit: Herkunft und Einstellblätter ────────────


class _CollectionAwareClient(_FakeClient):
    """Reicht Collection UND Filter durch – SEARCH_CNC sucht in zweien."""

    async def post(self, url, json=None, **kw):
        if url.endswith("/v1/embed"):
            return _FakeResp({"vector": [0.1, 0.2, 0.3], "model": "bge-m3"})
        if url.endswith("/v1/search"):
            return _FakeResp({"results": self._search_by_filter(
                json.get("collection"), json.get("filters") or {})})
        if url.endswith("/v1/scroll"):
            # ``scroll`` liefert Payloads statt Treffer – flach, mit text.
            hits = self._search_by_filter(json.get("collection"),
                                          json.get("filters") or {})
            return _FakeResp({"points": [
                {**(h.get("metadata") or {}), "text": h.get("text", "")}
                for h in hits
            ]})
        raise AssertionError(f"unexpected url {url}")


def _patch_client_collections(fn):
    return patch("core.actions.httpx.AsyncClient",
                 lambda *a, **k: _CollectionAwareClient(fn))


def _ruest_hit():
    return {
        "chunk_id": "eb1", "score": 0.7,
        "text": "[11221.3350 (Chiron 233-1) – Einstellblatt 1.pdf] Identnummer: 11221.3350",
        "metadata": {
            "file_name": "11221.3350 (Chiron 233-1) – Einstellblatt 1.pdf",
            "stored_path": "gw_stpoelten/ab/abcd_Einstellblatt 1.pdf",
            "page": 1,
            "extra": {"product_id": "11221.3350", "doc_kind": "einstellblatt"},
        },
    }


def _cnc_hit_with_source(**kw):
    hit = _cnc_hit_tid("T5", **kw)
    hit["text"] = ("Bearbeitungsschritt: Fasenfräsen.\nWerkzeug: FASENFRAESER, "
                   "Werkzeug-Nr. T5.\nSchnittparameter: Drehzahl 12000, Vorschub 4000.")
    hit["metadata"]["file_name"] = "112213350"
    hit["metadata"]["extra"]["program_name"] = "112213350"
    hit["metadata"]["extra"]["source_line"] = 139
    return hit


@pytest.mark.anyio
async def test_a_cited_tool_can_be_traced_back_to_its_program():
    """Ohne Herkunft steht in der Quellenansicht nur „– 1 Projekte"."""
    def by_col(collection, filters):
        if collection == "gw_cnc_steps":
            return [_cnc_hit_with_source(
                tool_type="FASENFRAESER", diameter=8.0, product_id="11221.3350",
                operation_type="Fasenfräsen", spindle_speed=12000, feed=4000)]
        return []

    with _patch_client_collections(by_col):
        res = await action_search_cnc(
            {"tool_id": "T5", "product_id": "11221.3350"}, use_case="gw_stpoelten")

    chunk = res["chunks"][0]
    assert chunk["metadata"]["file_name"] == "112213350"
    assert chunk["metadata"]["extra"]["source_line"] == 139
    assert "Drehzahl 12000" in chunk["text"]  # Originalzeile, nicht die Zusammenfassung
    assert "Programm 112213350" in res["observation"]


@pytest.mark.anyio
async def test_setup_sheets_are_offered_as_the_checkable_source():
    def by_col(collection, filters):
        if collection == "gw_cnc_steps":
            return [_cnc_hit_with_source(
                tool_type="FASENFRAESER", diameter=8.0, product_id="11221.3350",
                operation_type="Fasenfräsen")]
        return [_ruest_hit()]

    with _patch_client_collections(by_col):
        res = await action_search_cnc(
            {"tool_id": "T5", "product_id": "11221.3350"}, use_case="gw_stpoelten")

    assert "gw_ruest_data" in res["searched_collections"]
    assert "Einstellblatt 1.pdf, S. 1" in res["observation"]
    eb = res["chunks"][-1]
    assert eb["metadata"]["stored_path"].endswith("Einstellblatt 1.pdf")
    assert eb["metadata"]["page"] == 1


@pytest.mark.anyio
async def test_without_a_product_no_setup_sheets_are_pulled():
    """Einstellblätter ohne Bauteilbezug wären die Blätter irgendeines Teils."""
    seen: list[str] = []

    def by_col(collection, filters):
        seen.append(collection)
        return [_cnc_hit_with_source(
            tool_type="FASENFRAESER", diameter=8.0, product_id="11221.3350",
            operation_type="Fasenfräsen")]

    with _patch_client_collections(by_col):
        res = await action_search_cnc({"operation": "Fasenfräsen"},
                                      use_case="gw_stpoelten")

    assert "gw_ruest_data" not in seen
    assert res["searched_collections"] == ["gw_cnc_steps"]


# ── Werkzeug ohne Typ im Programm ────────────────────────────


@pytest.mark.anyio
async def test_a_tool_known_only_by_its_number_is_still_a_tool():
    """Im Betrieb der teuerste Fall: Der Dm63 Messerkopf steht im Programm
    von 11221.5277 als T20 ganz ohne Werkzeugtyp – nur „Planfräsen
    schruppen", S 7000, F 2000. Ihn zu verwerfen hiess dem Rüster sagen, das
    Werkzeug werde nicht verwendet, obwohl es im Einstellblatt unter
    derselben Nummer steht.
    """
    hit = _cnc_hit(tool_type=None, diameter=None, product_id="11221.5277",
                   operation_type="Planfräsen", spindle_speed=7000, feed=2000)
    hit["metadata"]["extra"]["tool_id"] = "T20"
    hit["metadata"]["extra"]["operation_label"] = "Planfräsen schruppen"

    with _patch_client(lambda has_mat: [hit]):
        res = await action_search_cnc({"operation": "Planfräsen",
                                       "product_id": "11221.5277"},
                                      use_case="gw_stpoelten")

    obs = res["observation"]
    assert "Werkzeug-Nr. T20" in obs
    assert "Typ im Programm nicht hinterlegt" in obs
    assert "Planfräsen schruppen" in obs  # Bezeichnung als einziger Hinweis
    assert "S 7000" in obs and "F 2000" in obs


@pytest.mark.anyio
async def test_numberless_typeless_steps_are_still_dropped():
    """Ohne Typ UND ohne Nummer bleibt nichts, was man rüsten könnte."""
    junk = _cnc_hit(tool_type=None, diameter=None, product_id="11221.5277",
                    operation_type="unbekannt")
    real = _cnc_hit(tool_type="HB SF", diameter=10.0, product_id="11221.5277",
                    operation_type="Taschenfräsen")

    with _patch_client(lambda has_mat: [junk, real]):
        res = await action_search_cnc({"operation": "Taschenfräsen"},
                                      use_case="gw_stpoelten")

    assert "unbekanntes Werkzeug" not in res["observation"]
    assert "Empfehlung: HB SF Ø10 mm" in res["observation"]

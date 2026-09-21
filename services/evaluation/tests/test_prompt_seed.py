"""Prompt-Seeding: Dateiänderungen wirken, Dashboard-Änderungen bleiben.

Das reine create-if-missing hat jede spätere Änderung an der Config unsichtbar
gemacht — die DB-Zeile existierte, also wurde die Datei nie wieder gelesen.
Prompt-Regeln wurden geschrieben, ausgerollt und erreichten keine einzige
Anfrage.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import core.seed as seed
import pytest


@pytest.fixture
def config(tmp_path):
    def _write(prompt: str):
        (tmp_path / "use_cases.json").write_text(json.dumps({"use_cases": [{
            "id": "gw_stpoelten", "slug": "gw", "label": "GW",
            "roles": ["default"], "agent_action_names": ["SEARCH", "FINAL_ANSWER"],
            "default_collection": "gw_cnc_steps", "collection_prefixes": ["gw_"],
            "prompts": {"default": prompt},
        }]}), encoding="utf-8")
        return tmp_path / "use_cases.json"
    return _write


def _patched(db_prompts: dict, upsert: AsyncMock):
    return (
        patch.object(seed, "list_deleted_use_cases", AsyncMock(return_value=set())),
        patch.object(seed, "get_use_case", AsyncMock(return_value=object())),
        patch.object(seed, "list_prompts", AsyncMock(return_value=db_prompts)),
        patch.object(seed, "upsert_prompt", upsert),
    )


async def _run(path, db_prompts):
    upsert = AsyncMock()
    a, b, c, d = _patched(db_prompts, upsert)
    with a, b, c, d:
        await seed.seed_use_cases(path)
    return upsert


@pytest.mark.anyio
async def test_first_seed_writes_prompt_and_fingerprint(config):
    path = config("Du bist ein CNC-Rüstexperte.")
    upsert = await _run(path, {})

    written = {c.args[1]: c.args[2] for c in upsert.await_args_list}
    assert written["agent.system.default"] == "Du bist ein CNC-Rüstexperte."
    assert written["agent.system.default.seeded_sha"] == seed._fingerprint(
        "Du bist ein CNC-Rüstexperte."
    )


@pytest.mark.anyio
async def test_file_change_reaches_the_database(config):
    """Der Fall, der zwei Tage lang niemandem aufgefallen ist."""
    alt = "Du bist ein CNC-Rüstexperte."
    neu = alt + "\n- Nennt der Nutzer eine Werkzeugnummer, gib sie als tool_id an."
    path = config(neu)

    upsert = await _run(path, {
        "agent.system.default": alt,
        "agent.system.default.seeded_sha": seed._fingerprint(alt),
    })

    written = {c.args[1]: c.args[2] for c in upsert.await_args_list}
    assert written["agent.system.default"] == neu
    assert written["agent.system.default.seeded_sha"] == seed._fingerprint(neu)


@pytest.mark.anyio
async def test_a_dashboard_edit_survives_a_file_change(config):
    path = config("Neue Fassung aus der Datei.")
    upsert = await _run(path, {
        "agent.system.default": "Von Hand im Dashboard geschärft.",
        "agent.system.default.seeded_sha": seed._fingerprint("Ursprünglich geseedet."),
    })

    written = {c.args[1]: c.args[2] for c in upsert.await_args_list}
    assert "agent.system.default" not in written


@pytest.mark.anyio
async def test_an_old_row_without_fingerprint_is_left_alone(config):
    """Ohne Fingerabdruck ist nicht zu entscheiden, wer den Text geschrieben
    hat. Dann gilt der Bestand — lieber eine wirkungslose Datei als ein
    überschriebener Prompt."""
    path = config("Neue Fassung aus der Datei.")
    upsert = await _run(path, {"agent.system.default": "Bestand unbekannter Herkunft."})

    written = {c.args[1]: c.args[2] for c in upsert.await_args_list}
    assert "agent.system.default" not in written


@pytest.mark.anyio
async def test_an_identical_row_gets_its_fingerprint_retrofitted(config):
    """Damit die nächste Dateiänderung durchkommt."""
    text = "Du bist ein CNC-Rüstexperte."
    path = config(text)
    upsert = await _run(path, {"agent.system.default": text})

    written = {c.args[1]: c.args[2] for c in upsert.await_args_list}
    assert written == {"agent.system.default.seeded_sha": seed._fingerprint(text)}

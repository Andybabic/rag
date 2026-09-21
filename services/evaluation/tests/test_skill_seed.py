"""Seeding a manifest that carries more than one reference skill.

The seed is create-if-missing, so the interesting cases are all about *not*
overwriting: a skill an admin already edited, a code file someone replaced, and
one broken entry that must not stop the others from being seeded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import core.skill_seed as seed
import pytest


@dataclass
class _Skill:
    id: str
    name: str
    has_code: bool = False


@pytest.fixture
def manifest(tmp_path):
    (tmp_path / "wetter.py").write_text(
        "# allow-network: api.open-meteo.com\ndef aktuell(ort):\n    return ort\n",
        encoding="utf-8",
    )
    (tmp_path / "lager.js").write_text(
        "// ask for permission\nexport async function buchen(artikel) { return artikel }\n",
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(json.dumps({"skills": [
        {"name": "wetter", "use_cases": ["*"], "code_file": "wetter.py",
         "overview": "Wetter", "detailed_task": "…"},
        {"name": "lager", "use_cases": ["neumann"], "code_file": "lager.js",
         "overview": "Lager", "detailed_task": "…"},
        {"name": "kaputt", "use_cases": ["*"], "code_file": "fehlt.py",
         "overview": "x", "detailed_task": "…"},
    ]}), encoding="utf-8")
    return tmp_path


@dataclass
class _UseCase:
    id: str


@pytest.fixture
def resolver(monkeypatch):
    """Two use cases, no skills yet."""
    state = {"skills": {"neumann": {}, "gw_stpoelten": {}}, "code": {}}

    async def list_use_cases(*, only_enabled=False):
        return [_UseCase("neumann"), _UseCase("gw_stpoelten")]

    async def list_skills(use_case):
        return list(state["skills"][use_case].values())

    async def create_skill(use_case, name, overview, detailed_task, **kw):
        skill = _Skill(id=f"{use_case}:{name}", name=name)
        state["skills"][use_case][name] = skill
        return skill

    async def set_skill_code(skill_id, *, language, file_name, source, functions):
        state["code"][skill_id] = {"language": language, "file": file_name,
                                   "functions": functions}
        return len(functions)

    monkeypatch.setattr(seed, "list_use_cases", list_use_cases)
    monkeypatch.setattr(seed, "list_skills", list_skills)
    monkeypatch.setattr(seed, "create_skill", AsyncMock(side_effect=create_skill))
    monkeypatch.setattr(seed, "set_skill_code", AsyncMock(side_effect=set_skill_code))
    return state


@pytest.mark.anyio
async def test_every_manifest_skill_lands_in_its_declared_use_cases(manifest, resolver):
    await seed.seed_skills(manifest)

    assert set(resolver["skills"]["neumann"]) == {"wetter", "lager"}
    # "lager" is scoped to one use case; "*" means every enabled one.
    assert set(resolver["skills"]["gw_stpoelten"]) == {"wetter"}


@pytest.mark.anyio
async def test_each_skill_keeps_its_own_language_and_flags(manifest, resolver):
    await seed.seed_skills(manifest)

    wetter = resolver["code"]["neumann:wetter"]
    lager = resolver["code"]["neumann:lager"]

    assert wetter["language"] == "python"
    assert wetter["functions"][0]["allowed_domains"] == ["api.open-meteo.com"]
    assert wetter["functions"][0]["ask_for_permission"] is False

    assert lager["language"] == "javascript"
    assert lager["functions"][0]["ask_for_permission"] is True
    assert lager["functions"][0]["allowed_domains"] == []


@pytest.mark.anyio
async def test_a_broken_entry_does_not_stop_the_others(manifest, resolver):
    """The third entry points at a file that does not exist."""
    imported = await seed.seed_skills(manifest)

    assert "kaputt" not in resolver["skills"]["neumann"]
    assert imported == 3, "wetter twice (two use cases) + lager once"


@pytest.mark.anyio
async def test_a_skill_that_already_has_its_own_code_is_left_alone(manifest, resolver):
    """An admin's replacement must survive the next restart."""
    resolver["skills"]["neumann"]["wetter"] = _Skill(
        id="neumann:wetter", name="wetter", has_code=True
    )

    await seed.seed_skills(manifest)

    assert "neumann:wetter" not in resolver["code"], "overwrote an edited skill"
    assert "gw_stpoelten:wetter" in resolver["code"], "other use cases still seeded"
    assert "neumann:lager" in resolver["code"], "other skills still seeded"


# ── Planung mit Werkzeugen ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_planner_is_told_about_the_tools(monkeypatch):
    """The decomposer has to know what exists beyond the document index.

    Without this it plans purely for retrieval, and a part of the question that
    only a tool can answer is dropped while decomposing — after which no
    sub-agent can rescue it, because the sub-task does not exist. Observed:
    "wird es regnen? welchen Bus…" produced two bus sub-tasks and silently lost
    the weather half.
    """
    from dataclasses import dataclass
    from unittest.mock import AsyncMock

    import core.manager as manager

    @dataclass
    class _Fn:
        skill_name: str
        name: str
        signature: str
        description: str
        skill_overview: str

    seen: dict = {}

    async def fake_llm(messages, **kw):
        seen["system"] = messages[0]["content"]
        return '{"rationale":"r","merge_strategy":"complementary","subtasks":[]}'

    monkeypatch.setattr(manager, "call_llm", AsyncMock(side_effect=fake_llm))

    await manager.plan_subtasks(
        "wird es regnen und welchen Bus nehme ich?",
        use_case="ustp", history=None,
        skill_functions=[_Fn("wetter", "vorhersage", "vorhersage(ort, tage)",
                             "Vorhersage für 1-7 Tage.",
                             "Live-Wetterdaten über Open-Meteo")],
    )

    system = seen["system"]
    assert "wetter.vorhersage(ort, tage)" in system
    assert "Live-Wetterdaten über Open-Meteo" in system
    assert "EIGENEN Sub-Task" in system, "the rule is what stops the drop"


@pytest.mark.anyio
async def test_planner_prompt_is_unchanged_without_tools(monkeypatch):
    """A use case with no functions must not be told about a tool section."""
    from unittest.mock import AsyncMock

    import core.manager as manager

    seen: dict = {}

    async def fake_llm(messages, **kw):
        seen["system"] = messages[0]["content"]
        return '{"rationale":"r","merge_strategy":"complementary","subtasks":[]}'

    monkeypatch.setattr(manager, "call_llm", AsyncMock(side_effect=fake_llm))
    await manager.plan_subtasks("x", use_case="ustp", history=None, skill_functions=[])

    assert "ZUSÄTZLICHE WERKZEUGE" not in seen["system"]

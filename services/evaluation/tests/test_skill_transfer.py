"""Export and import of skills.

The export is a file that lands in a download folder, gets mailed around and
committed to a repository, and the import brings in code this platform will
execute. Both halves of that are pinned here: what must never leave, and what
the importer must be told about what arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
import router.admin as admin


@dataclass
class _Skill:
    id: str
    use_case: str
    name: str
    overview: str = "o"
    detailed_task: str = "t"
    enabled: bool = True
    position: int = 0
    code_language: str | None = None
    code_file_name: str | None = None
    has_code: bool = False
    variable_count: int = 0


@dataclass
class _Fn:
    id: str
    skill_id: str
    skill_name: str
    name: str
    signature: str
    description: str = ""
    params: tuple = ()
    ask_for_permission: bool = False
    permission_from_code: bool = False
    permission_override: bool | None = None
    allowed_domains: tuple = ()
    timeout_ms: int = 10000
    memory_mb: int = 256
    skill_overview: str = ""


_CODE = (
    "# ask for permission\n"
    "# Legt einen Auftrag an.\n"
    "# allow-network: erp.example.com\n"
    "def auftrag_anlegen(anlage):\n"
    "    return anlage\n"
)


@pytest.fixture
def one_skill_with_secret():
    skill = _Skill(id="s1", use_case="ustp", name="wartung", has_code=True,
                   code_language="python", code_file_name="wartung.py",
                   variable_count=2)
    fn = _Fn(id="f1", skill_id="s1", skill_name="wartung", name="auftrag_anlegen",
             signature="auftrag_anlegen(anlage)", ask_for_permission=True,
             permission_from_code=True, allowed_domains=("erp.example.com",))
    with patch.object(admin, "list_skills", AsyncMock(return_value=[skill])), \
         patch.object(admin, "list_skill_functions", AsyncMock(return_value=[fn])), \
         patch.object(admin, "get_skill_code",
                      AsyncMock(return_value=("python", "wartung.py", _CODE))), \
         patch.object(admin, "get_skill_variables",
                      AsyncMock(return_value={"ERP_TOKEN": "streng-geheim",
                                              "ERP_MANDANT": "FIR-01"})):
        yield skill


# ── Export ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_export_never_carries_variable_values(one_skill_with_secret):
    """The one rule the export has: a stored key must not end up in a file."""
    doc = await admin.export_skills("ustp")
    blob = repr(doc)

    assert "streng-geheim" not in blob
    assert "FIR-01" not in blob
    # The names travel so the import side can say what still needs filling in.
    assert doc["skills"][0]["variable_names"] == ["ERP_MANDANT", "ERP_TOKEN"]


@pytest.mark.anyio
async def test_export_carries_the_code_and_its_risk_profile(one_skill_with_secret):
    entry = (await admin.export_skills("ustp"))["skills"][0]

    assert entry["code"] == _CODE
    assert entry["code_language"] == "python"
    assert entry["functions"] == [{
        "name": "auftrag_anlegen",
        "signature": "auftrag_anlegen(anlage)",
        "ask_for_permission": True,
        "permission_override": None,
        "allowed_domains": ("erp.example.com",),
    }]


# ── Import ───────────────────────────────────────────────────────────────────


@pytest.fixture
def empty_target():
    """A use case with no skills, recording what the import writes."""
    written: dict = {}

    async def create_skill(use_case, name, overview, detailed_task, **kw):
        return _Skill(id=f"new-{name}", use_case=use_case, name=name)

    async def set_skill_code(skill_id, *, language, file_name, source, functions):
        written[skill_id] = functions
        return len(functions)

    async def read_back(skill_id):
        return [
            _Fn(id="f1", skill_id=skill_id, skill_name="wartung",
                name=f["name"], signature=f["signature"],
                ask_for_permission=f["ask_for_permission"],
                permission_from_code=f["ask_for_permission"],
                allowed_domains=tuple(f["allowed_domains"]))
            for f in written.get(skill_id, [])
        ]

    with patch.object(admin, "list_skills", AsyncMock(return_value=[])), \
         patch.object(admin, "create_skill", AsyncMock(side_effect=create_skill)), \
         patch.object(admin, "set_skill_code", AsyncMock(side_effect=set_skill_code)), \
         patch.object(admin, "list_functions_for_skill", AsyncMock(side_effect=read_back)), \
         patch.object(admin, "get_skill_variables", AsyncMock(return_value={})):
        yield written


def _doc(**over):
    entry = {
        "name": "wartung", "overview": "o", "detailed_task": "t",
        "code_file_name": "wartung.py", "code": _CODE,
        "variable_names": ["ERP_TOKEN"],
    }
    entry.update(over)
    return admin.SkillImport(version=1, skills=[entry], overwrite=False)


@pytest.mark.anyio
async def test_import_reports_what_the_code_may_actually_do(empty_target):
    """Importing runs nothing — but it decides what may run later.

    The response is the only place an admin learns that the file they just
    imported contains a function that reaches an external host and one that
    asks before it runs.
    """
    result = await admin.import_skills("neumann", _doc())

    (entry,) = result["created"]
    assert entry["functions"] == ["auftrag_anlegen(anlage)"]
    assert entry["needs_permission"] == ["auftrag_anlegen(anlage)"]
    assert entry["reaches_hosts"] == ["erp.example.com"]
    # No value was imported for it — saying so beats a function failing later.
    assert entry["missing_variables"] == ["ERP_TOKEN"]


@pytest.mark.anyio
async def test_import_derives_permission_from_the_source_not_the_document():
    """The flag is re-parsed, never trusted from the file.

    A document claiming ask_for_permission=False for a function whose source
    carries the marker must not be able to turn the consent gate off.
    """
    written: dict = {}

    async def set_skill_code(skill_id, *, language, file_name, source, functions):
        written["functions"] = functions
        return len(functions)

    with patch.object(admin, "list_skills", AsyncMock(return_value=[])), \
         patch.object(admin, "create_skill",
                      AsyncMock(return_value=_Skill(id="s9", use_case="n", name="wartung"))), \
         patch.object(admin, "set_skill_code", AsyncMock(side_effect=set_skill_code)), \
         patch.object(admin, "list_functions_for_skill", AsyncMock(return_value=[])), \
         patch.object(admin, "get_skill_variables", AsyncMock(return_value={})):
        await admin.import_skills("neumann", _doc(functions=[
            {"name": "auftrag_anlegen", "ask_for_permission": False, "allowed_domains": []}
        ]))

    assert written["functions"][0]["ask_for_permission"] is True
    assert written["functions"][0]["allowed_domains"] == ["erp.example.com"]


@pytest.mark.anyio
async def test_existing_skill_is_skipped_without_overwrite():
    """A re-import must not silently discard local edits."""
    existing = _Skill(id="s1", use_case="neumann", name="wartung", has_code=True)
    setter = AsyncMock()
    with patch.object(admin, "list_skills", AsyncMock(return_value=[existing])), \
         patch.object(admin, "set_skill_code", setter), \
         patch.object(admin, "create_skill", AsyncMock()) as creator:
        result = await admin.import_skills("neumann", _doc())

    creator.assert_not_awaited()
    setter.assert_not_awaited()
    assert result["skipped"] == ["wartung"]


@pytest.mark.anyio
async def test_unparsable_code_fails_before_anything_is_written(empty_target):
    """A broken file must not leave a half-imported skill behind."""
    result = await admin.import_skills(
        "neumann", _doc(code="x = 1  # keine Funktion")
    )

    assert result["created"] == [] and result["updated"] == []
    assert result["failed"][0]["name"] == "wartung"
    assert empty_target == {}, "nothing may have been written"


@pytest.mark.anyio
async def test_unknown_version_is_refused():
    result = await admin.import_skills(
        "neumann", admin.SkillImport(version=99, skills=[])
    )
    assert result["error"] == "unsupported_version"


# ── Freigabe-Häkchen über Export/Import ──────────────────────────────────────


@pytest.mark.anyio
async def test_export_carries_the_admins_choice(one_skill_with_secret):
    """The override is not in the source, so the document has to carry it."""
    entry = (await admin.export_skills("ustp"))["skills"][0]
    assert "permission_override" in entry["functions"][0]


@pytest.mark.anyio
async def test_import_adopts_a_hardening_override_but_never_a_loosening_one():
    """Asymmetry on purpose.

    A document may switch a consent gate **on** — re-applying a decision
    someone made deliberately. It may never switch one **off**: a hand-edited
    file must not be able to strip the gate off a skill in transit, which is
    the one direction where being wrong is dangerous.
    """
    applied: list[tuple[str, bool | None]] = []

    async def record(skill_id, function_name, ask):
        applied.append((function_name, ask))
        return True

    async def read_back(skill_id):
        return []

    source_without_marker = "def offen(x):\n    return x\n"

    async def run(declared_override):
        applied.clear()
        with patch.object(admin, "list_skills", AsyncMock(return_value=[])), \
             patch.object(admin, "create_skill",
                          AsyncMock(return_value=_Skill(id="s1", use_case="n", name="w"))), \
             patch.object(admin, "set_skill_code", AsyncMock(return_value=1)), \
             patch.object(admin, "set_function_permission", AsyncMock(side_effect=record)), \
             patch.object(admin, "list_functions_for_skill", AsyncMock(side_effect=read_back)), \
             patch.object(admin, "get_skill_variables", AsyncMock(return_value={})):
            await admin.import_skills("neumann", admin.SkillImport(
                version=1, overwrite=True,
                skills=[{
                    "name": "w", "code_file_name": "w.py", "code": source_without_marker,
                    "functions": [{"name": "offen",
                                   "permission_override": declared_override}],
                }],
            ))

    await run(True)
    assert applied == [("offen", True)], "Verschärfung muss ankommen"

    await run(False)
    assert applied == [], "Abschwächung darf nichts verändern"


@pytest.mark.anyio
async def test_single_skill_export(one_skill_with_secret):
    """One skill, same document shape — so it imports through the same path."""
    doc = await admin.export_skills("ustp", skill_id="s1")
    assert [s["name"] for s in doc["skills"]] == ["wartung"]

    missing = await admin.export_skills("ustp", skill_id="gibtsnicht")
    assert missing["error"] == "not_found"

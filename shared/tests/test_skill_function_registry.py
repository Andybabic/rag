"""Resolver behaviour once a use case carries more than one skill.

With a single skill most of this is invisible: there is nothing to confuse a
lookup with, and the cache only ever holds one skill's functions. The moment a
use case has several — some enabled, some not, in different languages — the
registry has to keep them apart, and that is what these cover.
"""

from __future__ import annotations

import json

import pytest
from shared.usecase_config import resolver


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list[str] = []

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        # Mimic whichever WHERE clause the real query applies.
        if "f.skill_id = $1" in sql:
            return [r for r in self._rows if r["skill_id"] == args[0]]
        return [r for r in self._rows if r["use_case"] == args[0]]

    async def fetchrow(self, sql, *args):
        if "to_regclass" in sql:
            return {"ready": True}  # migration 0010 applied
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConn(rows)

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _row(skill, fn, *, enabled=True, use_case="neumann", permission=False,
         domains=(), skill_id=None):
    return {
        "id": f"{skill}-{fn}-id",
        "skill_id": skill_id or f"{skill}-id",
        "name": fn,
        "signature": f"{fn}(x)",
        "description": "",
        "params": json.dumps(["x"]),
        "ask_for_permission": permission,
        "allowed_domains": json.dumps(list(domains)),
        "timeout_ms": 10000,
        "memory_mb": 256,
        "skill_name": skill,
        "enabled": enabled,
        "use_case": use_case,
    }


@pytest.fixture
def registry(monkeypatch):
    """Two enabled skills plus one disabled, all in the same use case."""
    rows = [
        _row("wetter", "aktuell", domains=["api.open-meteo.com"]),
        _row("wetter", "vorhersage", domains=["api.open-meteo.com"]),
        _row("wartung", "auftrag_anlegen", permission=True),
        _row("altlast", "veraltet", enabled=False),
        _row("wetter", "aktuell", use_case="gw_stpoelten"),
    ]
    pool = _FakePool(rows)

    async def fake_pool():
        return pool

    monkeypatch.setattr(resolver, "_get_pool", fake_pool)
    resolver.invalidate_all()
    resolver.reset_plugin_schema_probe()
    return pool


@pytest.mark.anyio
async def test_enabled_functions_span_every_skill_of_the_use_case(registry):
    functions = await resolver.list_skill_functions("neumann")
    assert [f.qualified_name for f in functions] == [
        "wetter.aktuell",
        "wetter.vorhersage",
        "wartung.auftrag_anlegen",
    ]


@pytest.mark.anyio
async def test_per_function_flags_do_not_bleed_between_skills(registry):
    by_name = {f.qualified_name: f for f in await resolver.list_skill_functions("neumann")}

    assert by_name["wartung.auftrag_anlegen"].ask_for_permission is True
    assert by_name["wartung.auftrag_anlegen"].allowed_domains == []
    assert by_name["wetter.aktuell"].ask_for_permission is False
    assert by_name["wetter.aktuell"].allowed_domains == ["api.open-meteo.com"]


@pytest.mark.anyio
async def test_other_use_cases_are_not_visible(registry):
    functions = await resolver.list_skill_functions("gw_stpoelten")
    assert [f.qualified_name for f in functions] == ["wetter.aktuell"]


@pytest.mark.anyio
async def test_disabled_skill_is_still_readable_through_a_warm_cache(registry):
    """A warm cache must not hide a disabled skill from the admin view.

    The cache only ever stores the enabled-only list, because that is what the
    agent asks for on every request. Reading it back for an ``only_enabled=False``
    caller would silently drop exactly the skill the admin is looking at — and
    with several skills the cache is essentially always warm, kept alive by the
    active ones.
    """
    # Warm the cache the way a normal request does.
    await resolver.list_skill_functions("neumann")

    everything = await resolver.list_skill_functions("neumann", only_enabled=False)
    assert "altlast.veraltet" in {f.qualified_name for f in everything}


@pytest.mark.anyio
async def test_functions_of_one_skill_are_fetched_without_scanning_the_use_case(registry):
    functions = await resolver.list_functions_for_skill("wartung-id")
    assert [f.qualified_name for f in functions] == ["wartung.auftrag_anlegen"]


# ── Datenbank ohne die Plugin-Migration ──────────────────────────────────────


class _LegacyConn(_FakeConn):
    """A database that predates migration 0010.

    Selecting a plugin column raises the way asyncpg does, which is what an
    updated service meets when it is deployed against an older database —
    migrations are applied by hand in this project.
    """

    async def fetch(self, sql, *args):
        if "code_language" in sql or "usecase_skill_functions" in sql:
            raise RuntimeError('column "code_language" does not exist')
        self.queries.append(sql)
        return [r for r in self._rows if r.get("use_case") == args[0]]

    async def fetchrow(self, sql, *args):
        if "to_regclass" in sql:
            return {"ready": False}  # migration 0010 not applied
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


class _LegacyPool(_FakePool):
    def __init__(self, rows):
        self.conn = _LegacyConn(rows)


def _skill_row(name, use_case="neumann"):
    return {
        "id": f"{name}-id", "use_case": use_case, "name": name,
        "overview": "o", "detailed_task": "t", "enabled": True, "position": 0,
    }


@pytest.fixture
def legacy_db(monkeypatch):
    pool = _LegacyPool([_skill_row("quality_manager"), _skill_row("tonfall")])

    async def fake_pool():
        return pool

    monkeypatch.setattr(resolver, "_get_pool", fake_pool)
    resolver.invalidate_all()
    resolver.reset_plugin_schema_probe()
    return pool


@pytest.mark.anyio
async def test_existing_skills_survive_a_database_without_the_migration(legacy_db):
    """The compatibility promise: a skill without a function part keeps working.

    Asking for columns the database does not have made *every* skill read fail,
    and the failure was swallowed into an empty list — so an admin whose
    database had not been migrated saw "no skills defined" and their existing
    rules silently stopped reaching the prompt.
    """
    skills = await resolver.list_skills("neumann")

    assert [s.name for s in skills] == ["quality_manager", "tonfall"]
    assert all(s.has_code is False for s in skills)
    assert all(s.code_language is None for s in skills)


@pytest.mark.anyio
async def test_plugin_layer_reports_itself_unavailable_without_the_migration(legacy_db):
    assert await resolver.plugin_schema_ready() is False
    assert await resolver.list_skill_functions("neumann") == []
    assert await resolver.get_execution_bundle("neumann", "wetter", "aktuell") is None


# ── Zeilen-Mapping ───────────────────────────────────────────────────────────


class _AsyncpgLikeRecord(dict):
    """A row whose ``keys()`` is a one-shot iterator, like asyncpg's.

    ``asyncpg.Record.keys()`` returns a ``tuple_iterator``, not a view. Every
    ``in`` test against the *same* iterator consumes it up to the match, so a
    second test silently answers False. A plain dict hides this — its ``keys()``
    is a reusable view — which is why the bug survived the unit tests and only
    showed against a real database.
    """

    def keys(self):
        return iter(tuple(super().keys()))


def test_row_mapping_survives_a_one_shot_keys_iterator():
    row = _AsyncpgLikeRecord({
        "id": "s1", "use_case": "neumann", "name": "wetter", "overview": "o",
        "detailed_task": "t", "enabled": True, "position": 0,
        "code_language": "python", "code_file_name": "wetter.py",
        # Last on purpose: probing this first is what exhausted the iterator
        # and blanked every field tested after it.
        "has_code": True,
    })

    skill = resolver._row_to_skill(row)

    assert skill.has_code is True
    assert skill.code_language == "python"
    assert skill.code_file_name == "wetter.py"


def test_row_mapping_still_tolerates_a_pre_migration_row():
    row = _AsyncpgLikeRecord({
        "id": "s1", "use_case": "neumann", "name": "quality_manager",
        "overview": "o", "detailed_task": "t", "enabled": True, "position": 0,
    })

    skill = resolver._row_to_skill(row)

    assert skill.has_code is False
    assert skill.code_language is None


def test_function_mapping_survives_a_one_shot_keys_iterator():
    row = _AsyncpgLikeRecord({
        "id": "f1", "skill_id": "s1", "name": "aktuell", "signature": "aktuell(ort)",
        "description": "d", "params": json.dumps(["ort"]),
        "ask_for_permission": False, "allowed_domains": json.dumps([]),
        "timeout_ms": 10000, "memory_mb": 256, "skill_overview": "Wetterdaten",
    })

    function = resolver._row_to_function(row, "wetter")

    assert function.skill_overview == "Wetterdaten"


# ── Freigabe-Häkchen (Override aus dem Dashboard) ────────────────────────────


def _fn_row(name, *, from_code=False, override=None, skill="wetter"):
    return {
        "id": f"{name}-id", "skill_id": f"{skill}-id", "name": name,
        "signature": f"{name}(x)", "description": "", "params": json.dumps(["x"]),
        "ask_for_permission": from_code, "permission_override": override,
        "allowed_domains": json.dumps([]), "timeout_ms": 10000, "memory_mb": 256,
        "skill_name": skill, "skill_overview": "", "enabled": True,
        "use_case": "neumann",
    }


@pytest.mark.anyio
async def test_override_decides_the_effective_flag(monkeypatch):
    """`ask_for_permission` is the value the consent gate reads.

    Resolving the override here — rather than at each call site — is what makes
    the checkbox work everywhere without the gate, the prompt builder and the
    sandbox each needing to know about it.
    """
    rows = [
        _fn_row("a"),                                  # nichts gesetzt
        _fn_row("b", override=True),                   # im UI eingeschaltet
        _fn_row("c", from_code=True),                  # nur im Quelltext
        _fn_row("d", from_code=True, override=False),  # im UI abgeschaltet
    ]
    pool = _FakePool(rows)

    async def fake_pool():
        return pool

    monkeypatch.setattr(resolver, "_get_pool", fake_pool)
    resolver.invalidate_all()
    resolver.reset_plugin_schema_probe()

    by_name = {f.name: f for f in await resolver.list_skill_functions("neumann")}

    assert by_name["a"].ask_for_permission is False
    assert by_name["b"].ask_for_permission is True, "Häkchen setzen muss greifen"
    assert by_name["c"].ask_for_permission is True, "Markierung im Code greift weiter"
    assert by_name["d"].ask_for_permission is False, "Häkchen entfernen muss greifen"


@pytest.mark.anyio
async def test_source_flag_stays_visible_next_to_the_override(monkeypatch):
    """The UI shows "weicht vom Code ab", so both values have to survive."""
    pool = _FakePool([_fn_row("d", from_code=True, override=False)])

    async def fake_pool():
        return pool

    monkeypatch.setattr(resolver, "_get_pool", fake_pool)
    resolver.invalidate_all()
    resolver.reset_plugin_schema_probe()

    (fn,) = await resolver.list_skill_functions("neumann")
    assert (fn.ask_for_permission, fn.permission_from_code, fn.permission_override) == (
        False, True, False
    )

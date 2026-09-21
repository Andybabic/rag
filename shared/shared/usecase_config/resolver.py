"""Per-usecase configuration + prompt resolver (asyncpg-backed).

Pool management:
- Each service can pass in its own asyncpg pool via :func:`set_pool` (preferred,
  avoids two pools per process).
- Otherwise the resolver lazily creates one from ``DATABASE_URL`` on first use.

Cache:
- In-memory dict with a short TTL (60s). Invalidated explicitly by admin
  write paths via :func:`invalidate`.

Fallback:
- If the DB is unreachable or no row exists, ``resolve_config`` returns an
  :class:`shared.llm.LLMConfig` populated from env. ``resolve_prompt`` returns
  the supplied ``default``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

# ``set_pool`` is re-exported, not used here: callers still do
# ``from shared.usecase_config import set_pool``. The noqa keeps an
# "unused import" autofix from quietly breaking every one of them.
from shared.db import get_pool as _get_pool
from shared.db import set_pool  # noqa: F401
from shared.llm.config import LLMConfig
from shared.security.crypto import CryptoError, decrypt

logger = logging.getLogger(__name__)

GLOBAL_USE_CASE = "*"
_CACHE_TTL_SECONDS = 60.0

# ``set_pool``/``_get_pool`` now live in ``shared.db`` (shared across resolver
# and auth). They are imported above so existing
# ``from shared.usecase_config import set_pool`` keeps working.


# ── cache ────────────────────────────────────────────────────────────────────


_config_cache: dict[str, tuple[float, LLMConfig]] = {}
_prompt_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_skills_cache: dict[str, tuple[float, list["Skill"]]] = {}
_skill_functions_cache: dict[str, tuple[float, list["SkillFunction"]]] = {}


def invalidate(use_case: str) -> None:
    """Drop cached config + prompts + skills for one use case. Call after a write."""
    _config_cache.pop(use_case, None)
    _skills_cache.pop(use_case, None)
    _skill_functions_cache.pop(use_case, None)
    for key in list(_prompt_cache):
        if key[0] == use_case:
            del _prompt_cache[key]


def invalidate_all() -> None:
    _config_cache.clear()
    _prompt_cache.clear()
    _skills_cache.clear()
    _skill_functions_cache.clear()


# ── config ───────────────────────────────────────────────────────────────────


async def resolve_config(use_case: str | None = None) -> LLMConfig:
    """Return the effective LLMConfig for *use_case* (env fallbacks applied).

    If *use_case* is None or no row exists, behaves like ``LLMConfig.from_env()``.
    """
    base = LLMConfig.from_env()
    if not use_case:
        return base

    cached = _config_cache.get(use_case)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    pool = await _get_pool()
    if pool is None:
        _config_cache[use_case] = (time.monotonic(), base)
        return base

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM usecase_config WHERE use_case = $1",
                use_case,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("usecase_config read failed for %s: %s", use_case, exc)
        _config_cache[use_case] = (time.monotonic(), base)
        return base

    if row is None:
        _config_cache[use_case] = (time.monotonic(), base)
        return base

    cfg = _merge_row_into_config(base, row)
    _config_cache[use_case] = (time.monotonic(), cfg)
    return cfg


def _merge_row_into_config(base: LLMConfig, row: Any) -> LLMConfig:
    """Apply non-NULL columns from *row* on top of *base*."""
    overrides: dict[str, Any] = {}
    if row["chat_provider"]:
        overrides["chat_provider"] = row["chat_provider"].lower()
    if row["embedding_provider"]:
        overrides["embedding_provider"] = row["embedding_provider"].lower()
    if row["vision_provider"]:
        overrides["vision_provider"] = row["vision_provider"].lower()
    if row["ollama_base_url"]:
        overrides["ollama_base_url"] = row["ollama_base_url"]
    if row["openai_base_url"]:
        overrides["openai_base_url"] = row["openai_base_url"]

    if row["ollama_api_key_encrypted"]:
        try:
            overrides["ollama_api_key"] = decrypt(row["ollama_api_key_encrypted"])
        except CryptoError as exc:
            logger.warning("Cannot decrypt ollama_api_key: %s — using env value", exc)
    if row["openai_api_key_encrypted"]:
        try:
            overrides["openai_api_key"] = decrypt(row["openai_api_key_encrypted"])
        except CryptoError as exc:
            logger.warning("Cannot decrypt openai_api_key: %s — using env value", exc)

    # Model, tuning, and limit columns — any non-NULL DB value overrides env
    for col in (
        "llm_model", "embedding_model", "vision_model",
        "temperature", "max_tokens", "embed_batch_size",
        "agent_max_steps", "memory_max_chars", "embedding_dimension",
    ):
        if row[col] is not None:
            overrides[col] = row[col]

    return replace(base, **overrides) if overrides else base


async def upsert_config(use_case: str, fields: dict[str, Any]) -> None:
    """Upsert columns into ``usecase_config``. *fields* maps column → value.

    API-key fields must be passed pre-encrypted (column name ending in
    ``_encrypted``); pass an empty string to clear, ``None`` to leave unchanged.
    """
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist usecase_config")

    set_clauses: list[str] = []
    values: list[Any] = [use_case]
    insert_cols: list[str] = ["use_case"]
    insert_placeholders: list[str] = ["$1"]
    param_idx = 2
    for col, value in fields.items():
        if value is None:
            continue
        # empty string in an _encrypted field means "clear it"
        if isinstance(value, str) and value == "" and col.endswith("_encrypted"):
            set_clauses.append(f"{col} = NULL")
            continue
        set_clauses.append(f"{col} = ${param_idx}")
        insert_cols.append(col)
        insert_placeholders.append(f"${param_idx}")
        values.append(value)
        param_idx += 1

    if not set_clauses:
        return  # nothing to update

    set_clauses.append("updated_at = NOW()")
    sql = (
        f"INSERT INTO usecase_config ({', '.join(insert_cols)}) "
        f"VALUES ({', '.join(insert_placeholders)}) "
        f"ON CONFLICT (use_case) DO UPDATE SET {', '.join(set_clauses)}"
    )
    async with pool.acquire() as conn:
        await conn.execute(sql, *values)
    invalidate(use_case)


# ── prompts ──────────────────────────────────────────────────────────────────


async def resolve_prompt(
    use_case: str,
    prompt_key: str,
    *,
    default: str | None = None,
) -> str | None:
    """Return the stored prompt or *default* if none.

    Looks up ``(use_case, prompt_key)`` first, then falls back to the global
    row ``('*', prompt_key)``.
    """
    cache_key = (use_case, prompt_key)
    cached = _prompt_cache.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1] if cached[1] is not None else default

    pool = await _get_pool()
    value: str | None = None
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT content FROM usecase_prompts "
                    "WHERE prompt_key = $2 AND use_case = ANY($1::text[]) "
                    "ORDER BY CASE WHEN use_case = $3 THEN 0 ELSE 1 END "
                    "LIMIT 1",
                    [use_case, GLOBAL_USE_CASE],
                    prompt_key,
                    use_case,
                )
                value = row["content"] if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "usecase_prompts read failed for (%s, %s): %s",
                use_case,
                prompt_key,
                exc,
            )

    _prompt_cache[cache_key] = (time.monotonic(), value)
    return value if value is not None else default


async def list_prompts(use_case: str) -> dict[str, str]:
    """List all stored prompt keys for *use_case* (excluding global '*' rows).

    Returns ``{prompt_key: content}``.
    """
    pool = await _get_pool()
    if pool is None:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prompt_key, content FROM usecase_prompts WHERE use_case = $1",
            use_case,
        )
    return {r["prompt_key"]: r["content"] for r in rows}


async def upsert_prompt(use_case: str, prompt_key: str, content: str) -> None:
    """Persist a prompt override. Empty *content* deletes the row."""
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist usecase_prompts")

    async with pool.acquire() as conn:
        if content == "":
            await conn.execute(
                "DELETE FROM usecase_prompts WHERE use_case = $1 AND prompt_key = $2",
                use_case,
                prompt_key,
            )
        else:
            await conn.execute(
                "INSERT INTO usecase_prompts (use_case, prompt_key, content) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (use_case, prompt_key) DO UPDATE SET "
                "content = EXCLUDED.content, updated_at = NOW()",
                use_case,
                prompt_key,
                content,
            )
    invalidate(use_case)


# ── plugin-schema gate ───────────────────────────────────────────────────────
#
# Migrations are applied by hand in this project, so an updated service can
# legitimately meet a database that predates the plugin layer (0010). Asking
# such a database for ``code_language`` makes the *whole* skill read fail, and
# a swallowed failure turns into "no skills defined" — which would silently
# stop an admin's existing rules from reaching the prompt. Probing once and
# falling back to the pre-0010 column list keeps that promise: a skill without
# a function part works exactly as before, migrated or not.

_SKILL_COLUMNS = "id, use_case, name, overview, detailed_task, enabled, position"
_SKILL_COLUMNS_PLUGIN = (
    _SKILL_COLUMNS + ", code_language, code_file_name, "
    "(code IS NOT NULL AND code <> '') AS has_code, "
    # Count, never the values: the overview needs to show that variables exist
    # without any listing ever carrying an API key.
    "(SELECT count(*) FROM jsonb_object_keys(variables)) AS variable_count"
)

_plugin_schema: bool | None = None


def reset_plugin_schema_probe() -> None:
    """Forget the probe result. For tests, and after applying a migration."""
    global _plugin_schema
    _plugin_schema = None


async def plugin_schema_ready() -> bool:
    """True when migration 0010 has been applied to this database.

    Probed once per process: the answer only changes when someone runs a
    migration, and every skill read would otherwise pay for the check.
    """
    global _plugin_schema
    if _plugin_schema is not None:
        return _plugin_schema

    pool = await _get_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT to_regclass('public.usecase_skill_functions') IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM information_schema.columns "
                "            WHERE table_name = 'usecase_skills' "
                "              AND column_name = 'code_language') AS ready"
            )
        _plugin_schema = bool(row and row["ready"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Plugin schema probe failed: %s", exc)
        _plugin_schema = False

    if not _plugin_schema:
        logger.warning(
            "Skill plugin layer is inactive: this database predates migration "
            "0010. Existing skills keep working; run `make migrate` (or "
            "`alembic upgrade head` in db/) to enable executable functions."
        )
    return _plugin_schema


async def _skill_columns() -> str:
    return _SKILL_COLUMNS_PLUGIN if await plugin_schema_ready() else _SKILL_COLUMNS


# ── skills (per-usecase agent rules appended to the system prompt) ───────────


@dataclass(frozen=True)
class Skill:
    id: str
    use_case: str
    name: str
    overview: str
    detailed_task: str
    enabled: bool
    position: int
    # Plugin layer. ``has_code`` rather than the source itself: the agent reads
    # this list on every request and a skill file can be a quarter megabyte.
    # Fetch the source with :func:`get_skill_code` when it is actually needed.
    code_language: str | None = None
    code_file_name: str | None = None
    has_code: bool = False
    #: How many variables are stored — never which, never their values.
    variable_count: int = 0


def _row_to_skill(row: Any) -> Skill:
    # ``set()`` is load-bearing: asyncpg's ``Record.keys()`` is a one-shot
    # iterator, so testing several names against the same object consumes it
    # and every test after the first answers False. Probing ``has_code`` —
    # the last column — first blanked ``code_language`` and ``code_file_name``
    # on every row, while a plain dict in the tests hid it behind a reusable
    # key view.
    keys = set(row.keys())
    if "has_code" in keys:
        has_code = bool(row["has_code"])
    else:
        has_code = bool(row["code"]) if "code" in keys else False
    return Skill(
        id=str(row["id"]),
        use_case=row["use_case"],
        name=row["name"],
        overview=row["overview"],
        detailed_task=row["detailed_task"],
        enabled=row["enabled"],
        position=row["position"],
        code_language=row["code_language"] if "code_language" in keys else None,
        code_file_name=row["code_file_name"] if "code_file_name" in keys else None,
        has_code=has_code,
        variable_count=int(row["variable_count"] or 0) if "variable_count" in keys else 0,
    )


async def list_skills(use_case: str, *, only_enabled: bool = False) -> list[Skill]:
    """Return skills for *use_case*, ordered by ``(position, name)``.

    Cached briefly per use_case; the agent reads this on every request, so
    avoiding a DB roundtrip per query matters.
    """
    cached = _skills_cache.get(use_case)
    skills: list[Skill]
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        skills = cached[1]
    else:
        pool = await _get_pool()
        if pool is None:
            skills = []
        else:
            try:
                columns = await _skill_columns()
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        f"SELECT {columns} FROM usecase_skills "
                        "WHERE use_case = $1 ORDER BY position ASC, name ASC",
                        use_case,
                    )
                skills = [_row_to_skill(r) for r in rows]
            except Exception as exc:  # noqa: BLE001
                logger.warning("usecase_skills read failed for %s: %s", use_case, exc)
                skills = []
        _skills_cache[use_case] = (time.monotonic(), skills)

    if only_enabled:
        return [s for s in skills if s.enabled]
    return skills


async def get_skill(skill_id: str) -> Skill | None:
    pool = await _get_pool()
    if pool is None:
        return None
    columns = await _skill_columns()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {columns} FROM usecase_skills WHERE id = $1",
            skill_id,
        )
    return _row_to_skill(row) if row else None


async def create_skill(
    use_case: str,
    name: str,
    overview: str,
    detailed_task: str,
    *,
    enabled: bool = True,
    position: int = 0,
) -> Skill:
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist usecase_skills")
    columns = await _skill_columns()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO usecase_skills "
            "(use_case, name, overview, detailed_task, enabled, position) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            f"RETURNING {columns}",
            use_case,
            name,
            overview,
            detailed_task,
            enabled,
            position,
        )
    invalidate(use_case)
    return _row_to_skill(row)


async def update_skill(
    skill_id: str,
    *,
    name: str | None = None,
    overview: str | None = None,
    detailed_task: str | None = None,
    enabled: bool | None = None,
    position: int | None = None,
) -> Skill | None:
    """Patch update. Only fields explicitly provided are written."""
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist usecase_skills")

    sets: list[str] = []
    args: list[Any] = []
    for col, value in (
        ("name", name),
        ("overview", overview),
        ("detailed_task", detailed_task),
        ("enabled", enabled),
        ("position", position),
    ):
        if value is not None:
            args.append(value)
            sets.append(f"{col} = ${len(args)}")
    if not sets:
        return await get_skill(skill_id)

    sets.append("updated_at = NOW()")
    args.append(skill_id)
    columns = await _skill_columns()
    sql = (
        "UPDATE usecase_skills SET "
        + ", ".join(sets)
        + f" WHERE id = ${len(args)} RETURNING {columns}"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    if row is None:
        return None
    invalidate(row["use_case"])
    return _row_to_skill(row)


async def delete_skill(skill_id: str) -> bool:
    """Delete by id. Returns True if a row was removed."""
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist usecase_skills")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM usecase_skills WHERE id = $1 RETURNING use_case",
            skill_id,
        )
    if row is None:
        return False
    invalidate(row["use_case"])
    return True


# ── skill functions (the executable plugin layer) ────────────────────────────
#
# A skill carries at most one code file. Saving that file is the *import step*:
# the source is parsed once and the functions it declares are written to
# ``usecase_skill_functions``, permission flag and network allowlist included.
# Everything downstream — the prompt block the LLM sees, the call validator,
# the permission gate, the sandbox — reads those rows, never the source.


@dataclass(frozen=True)
class SkillFunction:
    """One callable function of a skill, as stored after the import step."""

    id: str
    skill_id: str
    skill_name: str
    name: str
    signature: str
    description: str
    params: list[str]
    #: The effective flag — the admin's choice if one was made, else the file's.
    #: Everything downstream (the consent gate, the prompt, the sandbox) reads
    #: this, so an override needs no special handling anywhere else.
    ask_for_permission: bool
    #: What the source file declares, for showing "weicht vom Code ab".
    permission_from_code: bool = False
    #: The admin's choice, or None when the file decides.
    permission_override: bool | None = None
    allowed_domains: list[str] = field(default_factory=list)
    timeout_ms: int = 10000
    memory_mb: int = 256
    #: The owning skill's one-line summary. Carried alongside the function so
    #: the prompt block can say what a skill is *for* — with several skills in
    #: a use case, a bare list of signatures gives the model no way to tell
    #: which one a question belongs to.
    skill_overview: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.skill_name}.{self.name}"


@dataclass(frozen=True)
class SkillExecutionBundle:
    """Everything the sandbox needs for exactly one execution.

    Assembled in a single query at call time. Carries the skill's variables,
    which is why it never leaves the evaluation service: it is handed straight
    to the sandbox and dropped.
    """

    skill_id: str
    skill_name: str
    use_case: str
    language: str
    code: str
    function: SkillFunction
    variables: dict[str, Any]


def _json_obj(value: Any) -> dict[str, Any]:
    """asyncpg hands back JSONB as a JSON string unless a codec is registered."""
    if value is None:
        return {}
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _row_to_function(row: Any, skill_name: str) -> SkillFunction:
    keys = set(row.keys())  # one-shot iterator on asyncpg — see _row_to_skill
    from_code = bool(row["ask_for_permission"])
    override = row["permission_override"] if "permission_override" in keys else None
    return SkillFunction(
        id=str(row["id"]),
        skill_id=str(row["skill_id"]),
        skill_name=skill_name,
        skill_overview=(row["skill_overview"] or "") if "skill_overview" in keys else "",
        name=row["name"],
        signature=row["signature"] or "",
        description=row["description"] or "",
        params=_json_list(row["params"]),
        # The override wins where one exists; otherwise the file decides.
        ask_for_permission=from_code if override is None else bool(override),
        permission_from_code=from_code,
        permission_override=None if override is None else bool(override),
        allowed_domains=_json_list(row["allowed_domains"]),
        timeout_ms=int(row["timeout_ms"]),
        memory_mb=int(row["memory_mb"]),
    )


async def list_skill_functions(
    use_case: str, *, only_enabled: bool = True
) -> list[SkillFunction]:
    """Every callable function of *use_case*, ordered by skill then position.

    Spans all of the use case's skills, which is what both hot paths need: the
    agent builds one prompt block from it, and the pipeline validates the calls
    that come back against the same set.

    Only the enabled-only result is cached, because that is the shape every
    request asks for — and therefore the cache is consulted only for that
    shape. Serving an ``only_enabled=False`` caller from it would silently drop
    the disabled skills it specifically asked for, and with several skills in a
    use case the cache is essentially always warm, kept alive by the active
    ones.
    """
    if not await plugin_schema_ready():
        return []
    cached = _skill_functions_cache.get(use_case) if only_enabled else None
    functions: list[SkillFunction]
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        functions = cached[1]
    else:
        pool = await _get_pool()
        if pool is None:
            functions = []
        else:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT f.id, f.skill_id, f.name, f.signature, f.description, "
                        "f.params, f.ask_for_permission, f.permission_override, "
                        "f.allowed_domains, "
                        "f.timeout_ms, f.memory_mb, s.name AS skill_name, "
                        "s.overview AS skill_overview, s.enabled "
                        "FROM usecase_skill_functions f "
                        "JOIN usecase_skills s ON s.id = f.skill_id "
                        "WHERE s.use_case = $1 "
                        "ORDER BY s.position ASC, s.name ASC, f.position ASC, f.name ASC",
                        use_case,
                    )
                functions = [
                    _row_to_function(r, r["skill_name"])
                    for r in rows
                    if r["enabled"] or not only_enabled
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "usecase_skill_functions read failed for %s: %s", use_case, exc
                )
                functions = []
        if only_enabled:
            _skill_functions_cache[use_case] = (time.monotonic(), functions)
    return functions


async def list_functions_for_skill(skill_id: str) -> list[SkillFunction]:
    """Every function of one skill, enabled or not.

    Addresses the skill directly instead of filtering the whole use case: the
    admin editor opens one skill at a time, and a use case may hold a dozen
    others whose sources are none of its business.
    """
    if not await plugin_schema_ready():
        return []
    pool = await _get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT f.id, f.skill_id, f.name, f.signature, f.description, "
                "f.params, f.ask_for_permission, f.permission_override, "
                "f.allowed_domains, "
                "f.timeout_ms, f.memory_mb, s.name AS skill_name, "
                "s.overview AS skill_overview "
                "FROM usecase_skill_functions f "
                "JOIN usecase_skills s ON s.id = f.skill_id "
                "WHERE f.skill_id = $1 "
                "ORDER BY f.position ASC, f.name ASC",
                skill_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill function read failed for %s: %s", skill_id, exc)
        return []
    return [_row_to_function(r, r["skill_name"]) for r in rows]


async def get_skill_code(skill_id: str) -> tuple[str, str, str] | None:
    """Return ``(language, file_name, source)`` for a skill, or None.

    Separate from :func:`get_skill` on purpose — the source is large and only
    the admin editor and the sandbox ever need it.
    """
    if not await plugin_schema_ready():
        return None
    pool = await _get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT code_language, code_file_name, code FROM usecase_skills "
            "WHERE id = $1",
            skill_id,
        )
    if row is None or not row["code"]:
        return None
    return (row["code_language"] or "", row["code_file_name"] or "", row["code"])


async def get_execution_bundle(
    use_case: str, skill_name: str, function_name: str
) -> SkillExecutionBundle | None:
    """Resolve one ``skill.function`` call to everything needed to run it.

    Returns None when the skill does not exist for this use case, is disabled,
    carries no code, or declares no such function — which is exactly the
    "not resolvable, do not execute" case from the spec. Skill lookup is
    case-insensitive because models do not reliably reproduce casing.
    """
    if not await plugin_schema_ready():
        return None
    pool = await _get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT s.id AS skill_id, s.name AS skill_name, s.use_case, "
            "s.code_language, s.code, s.variables, "
            "f.id, f.skill_id AS f_skill_id, f.name, f.signature, f.description, "
            "f.params, f.ask_for_permission, f.permission_override, "
            "f.allowed_domains, f.timeout_ms, f.memory_mb "
            "FROM usecase_skills s "
            "JOIN usecase_skill_functions f ON f.skill_id = s.id "
            "WHERE s.use_case = $1 AND lower(s.name) = lower($2) "
            "AND lower(f.name) = lower($3) AND s.enabled = TRUE "
            "AND s.code IS NOT NULL AND s.code <> ''",
            use_case,
            skill_name,
            function_name,
        )
    if row is None:
        return None
    from_code = bool(row["ask_for_permission"])
    override = row["permission_override"]
    function = SkillFunction(
        id=str(row["id"]),
        skill_id=str(row["f_skill_id"]),
        skill_name=row["skill_name"],
        name=row["name"],
        signature=row["signature"] or "",
        description=row["description"] or "",
        params=_json_list(row["params"]),
        # The gate reads this one value, so the override has to be resolved
        # here — not somewhere the execution path might skip.
        ask_for_permission=from_code if override is None else bool(override),
        permission_from_code=from_code,
        permission_override=None if override is None else bool(override),
        allowed_domains=_json_list(row["allowed_domains"]),
        timeout_ms=int(row["timeout_ms"]),
        memory_mb=int(row["memory_mb"]),
    )
    return SkillExecutionBundle(
        skill_id=str(row["skill_id"]),
        skill_name=row["skill_name"],
        use_case=row["use_case"],
        language=row["code_language"] or "",
        code=row["code"] or "",
        function=function,
        variables=_json_obj(row["variables"]),
    )


async def set_skill_code(
    skill_id: str,
    *,
    language: str,
    file_name: str,
    source: str,
    functions: list[dict[str, Any]],
) -> int:
    """Store a skill's code file and replace its derived function table.

    *functions* comes from ``shared.skills.parse_skill_code`` — the caller
    parses, this writes. Source and function rows are written in one
    transaction so a skill can never be live with code that does not match its
    permission flags.

    Returns the number of functions written.
    """
    import json

    if not await plugin_schema_ready():
        raise RuntimeError(
            "The skill plugin layer needs migration 0010. Run `make migrate` "
            "(or `alembic upgrade head` in db/) first."
        )
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist skill code")

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE usecase_skills SET code_language = $1, code_file_name = $2, "
                "code = $3, updated_at = NOW() WHERE id = $4 RETURNING use_case",
                language,
                file_name,
                source,
                skill_id,
            )
            if row is None:
                raise LookupError(f"Skill {skill_id} not found")
            # An admin's explicit consent choice is not part of the file, so it
            # has to be carried across the rewrite by name. Without this,
            # re-saving the source would quietly switch a gate back off.
            overrides = {
                r["name"]: r["permission_override"]
                for r in await conn.fetch(
                    "SELECT name, permission_override FROM usecase_skill_functions "
                    "WHERE skill_id = $1 AND permission_override IS NOT NULL",
                    skill_id,
                )
            }
            # Replace rather than merge: a function deleted from the file must
            # stop being callable, and a permission flag removed from the file
            # must not survive in a stale row.
            await conn.execute(
                "DELETE FROM usecase_skill_functions WHERE skill_id = $1", skill_id
            )
            for position, fn in enumerate(functions):
                await conn.execute(
                    "INSERT INTO usecase_skill_functions "
                    "(skill_id, name, signature, description, params, "
                    " ask_for_permission, permission_override, allowed_domains, "
                    " timeout_ms, memory_mb, position) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8::jsonb, $9, "
                    "        $10, $11)",
                    skill_id,
                    fn["name"],
                    fn.get("signature", ""),
                    fn.get("description", ""),
                    json.dumps(list(fn.get("params") or [])),
                    bool(fn.get("ask_for_permission")),
                    overrides.get(fn["name"]),
                    json.dumps(list(fn.get("allowed_domains") or [])),
                    int(fn.get("timeout_ms") or 10000),
                    int(fn.get("memory_mb") or 256),
                    position,
                )
    invalidate(row["use_case"])
    return len(functions)


async def set_function_permission(
    skill_id: str, function_name: str, ask: bool | None
) -> bool:
    """Set (or clear) the consent override for one function.

    ``ask=None`` hands the decision back to the source file. Returns False when
    the function does not exist.

    The write goes to the same row the gate reads, so it takes effect on the
    next request — a consent setting that needed a restart to apply would be a
    setting nobody trusts.
    """
    if not await plugin_schema_ready():
        raise RuntimeError(
            "The skill plugin layer needs migration 0011. Run `make migrate` "
            "(or `alembic upgrade head` in db/) first."
        )
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist the override")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE usecase_skill_functions f SET permission_override = $1 "
            "FROM usecase_skills s "
            "WHERE f.skill_id = s.id AND f.skill_id = $2 AND f.name = $3 "
            "RETURNING s.use_case",
            ask,
            skill_id,
            function_name,
        )
    if row is None:
        return False
    invalidate(row["use_case"])
    return True


async def clear_skill_code(skill_id: str) -> bool:
    """Drop a skill's code file and every function it declared."""
    if not await plugin_schema_ready():
        raise RuntimeError(
            "The skill plugin layer needs migration 0010. Run `make migrate` "
            "(or `alembic upgrade head` in db/) first."
        )
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist skill code")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE usecase_skills SET code_language = NULL, "
                "code_file_name = NULL, code = NULL, updated_at = NOW() "
                "WHERE id = $1 RETURNING use_case",
                skill_id,
            )
            if row is None:
                return False
            await conn.execute(
                "DELETE FROM usecase_skill_functions WHERE skill_id = $1", skill_id
            )
    invalidate(row["use_case"])
    return True


# ── skill variables (restricted access) ──────────────────────────────────────
#
# Values are readable through exactly two paths: the execution step, which
# hands them to the sandbox and forgets them, and the admin-only endpoint.
# They are deliberately absent from ``Skill`` so that no ordinary skill
# listing can leak an API key by accident.


async def get_skill_variables(skill_id: str) -> dict[str, Any]:
    """Raw variable values. Callers must already have checked authorisation."""
    if not await plugin_schema_ready():
        return {}
    pool = await _get_pool()
    if pool is None:
        return {}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT variables FROM usecase_skills WHERE id = $1", skill_id
        )
    return _json_obj(row["variables"]) if row else {}


async def set_skill_variables(skill_id: str, variables: dict[str, Any]) -> bool:
    """Replace a skill's whole variable object. Returns False if unknown id."""
    import json

    if not await plugin_schema_ready():
        raise RuntimeError(
            "The skill plugin layer needs migration 0010. Run `make migrate` "
            "(or `alembic upgrade head` in db/) first."
        )
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist skill variables")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE usecase_skills SET variables = $1::jsonb, updated_at = NOW() "
            "WHERE id = $2 RETURNING use_case",
            json.dumps(variables),
            skill_id,
        )
    if row is None:
        return False
    invalidate(row["use_case"])
    return True


# ── execution audit ──────────────────────────────────────────────────────────


async def log_skill_execution(
    *,
    use_case: str,
    skill_name: str,
    function_name: str,
    status: str,
    skill_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    username: str | None = None,
    args: list[Any] | None = None,
    permission_required: bool = False,
    permission_granted: bool | None = None,
    error: str | None = None,
    result_bytes: int = 0,
    duration_ms: int = 0,
) -> None:
    """Record one attempted execution. Never raises — auditing must not break
    an answer that already worked."""
    import json

    try:
        if not await plugin_schema_ready():
            return
        pool = await _get_pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO skill_executions "
                "(skill_id, use_case, skill_name, function_name, session_id, "
                " request_id, username, args, permission_required, "
                " permission_granted, status, error, result_bytes, duration_ms) "
                "VALUES ($1::uuid, $2, $3, $4, $5::uuid, $6::uuid, $7, $8::jsonb, "
                "        $9, $10, $11, $12, $13, $14)",
                skill_id,
                use_case,
                skill_name,
                function_name,
                session_id,
                request_id,
                username,
                json.dumps(args or []),
                permission_required,
                permission_granted,
                status,
                (error or "")[:2000] or None,
                result_bytes,
                duration_ms,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill_executions write failed: %s", exc)


async def list_skill_executions(use_case: str, *, limit: int = 100) -> list[dict]:
    """Most recent audit rows for a use case, newest first."""
    if not await plugin_schema_ready():
        return []
    pool = await _get_pool()
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, skill_name, function_name, session_id, request_id, "
            "username, args, permission_required, permission_granted, status, "
            "error, result_bytes, duration_ms, created_at "
            "FROM skill_executions WHERE use_case = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            use_case,
            min(max(limit, 1), 500),
        )
    return [
        {
            "id": str(r["id"]),
            "skill_name": r["skill_name"],
            "function_name": r["function_name"],
            "session_id": str(r["session_id"]) if r["session_id"] else None,
            "request_id": str(r["request_id"]) if r["request_id"] else None,
            "username": r["username"],
            "args": _json_list(r["args"]),
            "permission_required": r["permission_required"],
            "permission_granted": r["permission_granted"],
            "status": r["status"],
            "error": r["error"],
            "result_bytes": r["result_bytes"],
            "duration_ms": r["duration_ms"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


# ── use cases (DB-backed registry) ───────────────────────────────────────────
#
# The use-case registry (roles, agent actions, default collection, frontend
# metadata) is seeded from ``config/use_cases.json`` at boot and lives in the
# ``use_cases`` table afterwards, so it can be edited via the dashboard. Prompts
# stay in ``usecase_prompts`` (see ``resolve_prompt``).


@dataclass(frozen=True)
class UseCase:
    id: str  # API id, e.g. "gw_stpoelten" — also the `use_case` key everywhere
    slug: str  # URL path, e.g. "gw-stpoelten"
    label: str
    description: str
    color: str  # Tailwind bg class, e.g. "bg-emerald-600"
    accent: str  # hex colour for borders etc.
    roles: list[str]
    agent_action_names: list[str]
    default_collection: str
    collection_prefixes: list[str]
    enabled: bool


# cache: a single snapshot of all use cases (small table, read on every request)
_use_cases_cache: tuple[float, list[UseCase]] | None = None


def invalidate_use_cases() -> None:
    """Drop the cached use-case list. Call after any use-case write."""
    global _use_cases_cache
    _use_cases_cache = None


def _row_to_use_case(row: Any) -> UseCase:
    return UseCase(
        id=row["id"],
        slug=row["slug"],
        label=row["label"],
        description=row["description"] or "",
        color=row["color"] or "",
        accent=row["accent"] or "",
        roles=_json_list(row["roles"]),
        agent_action_names=_json_list(row["agent_action_names"]),
        default_collection=row["default_collection"] or "",
        collection_prefixes=_json_list(row["collection_prefixes"]),
        enabled=row["enabled"],
    )


def _json_list(value: Any) -> list[str]:
    """asyncpg returns JSONB as a JSON-encoded string by default."""
    if value is None:
        return []
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return list(value) if isinstance(value, (list, tuple)) else []


async def list_use_cases(*, only_enabled: bool = False) -> list[UseCase]:
    """Return all use cases ordered by id. Cached briefly.

    Returns ``[]`` if the DB is unreachable (callers fall back to file defaults).
    """
    global _use_cases_cache
    if _use_cases_cache and (time.monotonic() - _use_cases_cache[0]) < _CACHE_TTL_SECONDS:
        cases = _use_cases_cache[1]
    else:
        pool = await _get_pool()
        if pool is None:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, slug, label, description, color, accent, roles, "
                    "agent_action_names, default_collection, collection_prefixes, "
                    "enabled FROM use_cases ORDER BY id ASC"
                )
            cases = [_row_to_use_case(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("use_cases read failed: %s", exc)
            return []
        _use_cases_cache = (time.monotonic(), cases)

    if only_enabled:
        return [uc for uc in cases if uc.enabled]
    return cases


async def get_use_case(use_case_id: str) -> UseCase | None:
    pool = await _get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, slug, label, description, color, accent, roles, "
            "agent_action_names, default_collection, collection_prefixes, "
            "enabled FROM use_cases WHERE id = $1",
            use_case_id,
        )
    return _row_to_use_case(row) if row else None


async def upsert_use_case(
    use_case_id: str,
    *,
    slug: str,
    label: str,
    description: str = "",
    color: str = "",
    accent: str = "",
    roles: list[str],
    agent_action_names: list[str],
    default_collection: str,
    collection_prefixes: list[str] | None = None,
    enabled: bool = True,
) -> UseCase:
    """Insert or update a full use-case row. Returns the stored row."""
    import json

    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist use_cases")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO use_cases "
            "(id, slug, label, description, color, accent, roles, "
            " agent_action_names, default_collection, collection_prefixes, enabled) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  slug = EXCLUDED.slug, label = EXCLUDED.label, "
            "  description = EXCLUDED.description, color = EXCLUDED.color, "
            "  accent = EXCLUDED.accent, roles = EXCLUDED.roles, "
            "  agent_action_names = EXCLUDED.agent_action_names, "
            "  default_collection = EXCLUDED.default_collection, "
            "  collection_prefixes = EXCLUDED.collection_prefixes, "
            "  enabled = EXCLUDED.enabled, updated_at = NOW() "
            "RETURNING id, slug, label, description, color, accent, roles, "
            "agent_action_names, default_collection, collection_prefixes, enabled",
            use_case_id,
            slug,
            label,
            description,
            color,
            accent,
            json.dumps(roles),
            json.dumps(agent_action_names),
            default_collection,
            json.dumps(collection_prefixes or []),
            enabled,
        )
        # Re-creating a use case clears any tombstone so it's treated as live
        # again (and the boot seed won't skip it).
        await conn.execute(
            "DELETE FROM deleted_use_cases WHERE use_case = $1", use_case_id
        )
    invalidate(use_case_id)
    invalidate_use_cases()
    return _row_to_use_case(row)


async def delete_use_case(use_case_id: str) -> bool:
    """Delete a use case by id. Returns True if a row was removed.

    Records a tombstone so the boot-time seed does not resurrect the use case
    on the next restart. Does not cascade to prompts/config/skills — those are
    keyed by the same ``use_case`` string and can be cleaned up separately.
    """
    pool = await _get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot persist use_cases")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM use_cases WHERE id = $1 RETURNING id",
                use_case_id,
            )
            await conn.execute(
                "INSERT INTO deleted_use_cases (use_case) VALUES ($1) "
                "ON CONFLICT (use_case) DO NOTHING",
                use_case_id,
            )
    invalidate(use_case_id)
    invalidate_use_cases()
    return row is not None


async def list_deleted_use_cases() -> set[str]:
    """Return the set of use-case ids that were deleted (tombstoned).

    Degrades to an empty set if the DB is unavailable or the tombstone table
    doesn't exist yet (un-migrated DB), so the boot seed never fails on it.
    """
    pool = await _get_pool()
    if pool is None:
        return set()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT use_case FROM deleted_use_cases")
    except Exception as exc:  # noqa: BLE001 — missing table / DB hiccup → no tombstones
        logger.warning("Could not read deleted_use_cases (%s); assuming none", exc)
        return set()
    return {r["use_case"] for r in rows}

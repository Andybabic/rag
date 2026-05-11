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
import os
import time
from dataclasses import dataclass, replace
from typing import Any

from shared.llm.config import LLMConfig
from shared.security.crypto import CryptoError, decrypt

logger = logging.getLogger(__name__)

GLOBAL_USE_CASE = "*"
_CACHE_TTL_SECONDS = 60.0


# ── pool management ──────────────────────────────────────────────────────────


_pool: Any | None = None  # asyncpg.Pool, kept untyped to avoid hard import


def set_pool(pool: Any) -> None:
    """Inject an existing asyncpg pool. Call this from your service startup."""
    global _pool
    _pool = pool


async def _get_pool() -> Any | None:
    global _pool
    if _pool is not None:
        return _pool
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    try:
        import asyncpg  # local import: services without DB don't pay the cost
    except ImportError:
        logger.warning("asyncpg is not installed; usecase config will use .env only")
        return None
    try:
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
        return _pool
    except Exception as exc:  # noqa: BLE001 — DB unreachable is non-fatal
        logger.warning("Could not create resolver DB pool: %s", exc)
        return None


# ── cache ────────────────────────────────────────────────────────────────────


_config_cache: dict[str, tuple[float, LLMConfig]] = {}
_prompt_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_skills_cache: dict[str, tuple[float, list["Skill"]]] = {}


def invalidate(use_case: str) -> None:
    """Drop cached config + prompts + skills for one use case. Call after a write."""
    _config_cache.pop(use_case, None)
    _skills_cache.pop(use_case, None)
    for key in list(_prompt_cache):
        if key[0] == use_case:
            del _prompt_cache[key]


def invalidate_all() -> None:
    _config_cache.clear()
    _prompt_cache.clear()
    _skills_cache.clear()


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
    for i, (col, value) in enumerate(fields.items(), start=2):
        if value is None:
            continue
        # empty string in an _encrypted field means "clear it"
        if isinstance(value, str) and value == "" and col.endswith("_encrypted"):
            value = None
        set_clauses.append(f"{col} = ${i}")
        insert_cols.append(col)
        insert_placeholders.append(f"${i}")
        values.append(value)

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


def _row_to_skill(row: Any) -> Skill:
    return Skill(
        id=str(row["id"]),
        use_case=row["use_case"],
        name=row["name"],
        overview=row["overview"],
        detailed_task=row["detailed_task"],
        enabled=row["enabled"],
        position=row["position"],
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
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT id, use_case, name, overview, detailed_task, "
                        "enabled, position FROM usecase_skills "
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
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, use_case, name, overview, detailed_task, enabled, position "
            "FROM usecase_skills WHERE id = $1",
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
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO usecase_skills "
            "(use_case, name, overview, detailed_task, enabled, position) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "RETURNING id, use_case, name, overview, detailed_task, enabled, position",
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
    sql = (
        "UPDATE usecase_skills SET "
        + ", ".join(sets)
        + f" WHERE id = ${len(args)} "
        "RETURNING id, use_case, name, overview, detailed_task, enabled, position"
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

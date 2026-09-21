"""Admin API – query history, memory, prompts, per-usecase config."""


from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from core.api_tokens import ApiToken
from core.api_tokens import create_token as create_api_token
from core.api_tokens import delete_token as delete_api_token
from core.api_tokens import list_tokens as list_api_tokens
from core.api_tokens import revoke_token as revoke_api_token
from core.database import get_pool
from core.memory import read_memory, write_memory
from core.prompts import ACTION_CATALOG
from core.provider_test import list_chat_models, overlay_config, run_connection_tests
from core.use_cases import (
    GLOBAL_USE_CASE,
    PROMPT_KEY_REACT_SUFFIX,
    get_agent_actions_full,
    get_react_suffix,
    get_system_prompt,
    refresh_use_cases,
    set_action_description,
    set_action_enabled,
    set_react_suffix,
    set_system_prompt,
)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from shared.llm.config import LLMConfig
from shared.security.crypto import CryptoError, encrypt, is_configured
from shared.skills import (
    SkillCodeError,
    detect_language,
    parse_skill_code,
)
from shared.usecase_config import (
    Skill,
    SkillFunction,
    clear_skill_code,
    create_skill,
    delete_skill,
    delete_use_case,
    get_skill,
    get_skill_code,
    get_skill_variables,
    get_use_case,
    list_functions_for_skill,
    list_prompts,
    list_skill_executions,
    list_skill_functions,
    list_skills,
    list_use_cases,
    resolve_config,
    set_function_permission,
    set_skill_code,
    set_skill_variables,
    update_skill,
    upsert_config,
    upsert_prompt,
    upsert_use_case,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ── Query History ─────────────────────────────────────────────


@router.get("/queries")
async def list_queries(use_case: str = "", limit: int = 50, offset: int = 0):
    """List past queries, optionally filtered by use_case."""
    pool = await get_pool()
    if use_case:
        rows = await pool.fetch(
            """SELECT id, use_case, session_id, role, query_text, answer_text,
                      agent_steps, citations, images, sufficient, created_at
               FROM queries
               WHERE use_case = $1
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            use_case,
            limit,
            offset,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, use_case, session_id, role, query_text, answer_text,
                      agent_steps, citations, images, sufficient, created_at
               FROM queries
               ORDER BY created_at DESC
               LIMIT $1 OFFSET $2""",
            limit,
            offset,
        )
    return {
        "queries": [
            {
                "id": str(r["id"]),
                "use_case": r["use_case"],
                "session_id": str(r["session_id"]) if r["session_id"] else None,
                "role": r["role"],
                "query_text": r["query_text"],
                "answer_text": r["answer_text"],
                "agent_steps": json.loads(r["agent_steps"]) if r["agent_steps"] else [],
                "citations": json.loads(r["citations"]) if r["citations"] else [],
                "images": json.loads(r["images"]) if r["images"] else [],
                "sufficient": r["sufficient"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


# ── Feedback ──────────────────────────────────────────────────


@router.get("/feedback")
async def list_feedback(use_case: str = "", rating: str = "", limit: int = 100, offset: int = 0):
    """List user feedback, newest first, joined with the rated query.

    Optional filters: ``use_case`` and ``rating`` ('positive'/'negative').
    """
    pool = await get_pool()
    conditions = []
    params: list = []
    if use_case:
        params.append(use_case)
        conditions.append(f"q.use_case = ${len(params)}")
    if rating in ("positive", "negative"):
        params.append(rating)
        conditions.append(f"f.rating = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    params.append(limit)
    limit_ph = f"${len(params)}"
    params.append(offset)
    offset_ph = f"${len(params)}"

    rows = await pool.fetch(
        f"""SELECT f.id, f.query_id, f.rating, f.comment, f.created_at,
                   q.use_case, q.session_id, q.role, q.query_text, q.answer_text
            FROM feedback f
            LEFT JOIN queries q ON q.id = f.query_id
            {where}
            ORDER BY f.created_at DESC
            LIMIT {limit_ph} OFFSET {offset_ph}""",
        *params,
    )
    return {
        "feedback": [
            {
                "id": str(r["id"]),
                "query_id": str(r["query_id"]) if r["query_id"] else None,
                "rating": r["rating"],
                "comment": r["comment"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "use_case": r["use_case"],
                "session_id": str(r["session_id"]) if r["session_id"] else None,
                "role": r["role"],
                "query_text": r["query_text"],
                "answer_text": r["answer_text"],
            }
            for r in rows
        ]
    }


@router.get("/chat/{session_id}")
async def get_chat(session_id: str):
    """Return a full conversation (all queries of a session, oldest first)
    together with any feedback, so the dashboard can export it for traceability.
    """
    pool = await get_pool()
    try:
        rows = await pool.fetch(
            """SELECT q.id, q.use_case, q.session_id, q.role, q.query_text,
                      q.answer_text, q.agent_steps, q.citations, q.images,
                      q.sufficient, q.created_at,
                      f.rating, f.comment, f.created_at AS feedback_at
               FROM queries q
               LEFT JOIN feedback f ON f.query_id = q.id
               WHERE q.session_id = $1::uuid
               ORDER BY q.created_at ASC""",
            session_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid session_id: {exc}")

    return {
        "session_id": session_id,
        "messages": [
            {
                "id": str(r["id"]),
                "use_case": r["use_case"],
                "role": r["role"],
                "query_text": r["query_text"],
                "answer_text": r["answer_text"],
                "agent_steps": json.loads(r["agent_steps"]) if r["agent_steps"] else [],
                "citations": json.loads(r["citations"]) if r["citations"] else [],
                "images": json.loads(r["images"]) if r["images"] else [],
                "sufficient": r["sufficient"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "rating": r["rating"],
                "comment": r["comment"],
                "feedback_at": r["feedback_at"].isoformat() if r["feedback_at"] else None,
            }
            for r in rows
        ],
    }


# ── Memory ────────────────────────────────────────────────────


@router.get("/memory/{use_case}")
async def get_memory(use_case: str):
    """Read agent memory for a use case."""
    text = await read_memory(use_case)
    return {"use_case": use_case, "memory_text": text}


class MemoryUpdate(BaseModel):
    memory_text: str


@router.put("/memory/{use_case}")
async def update_memory(use_case: str, body: MemoryUpdate):
    """Update agent memory for a use case."""
    await write_memory(use_case, body.memory_text)
    return {"status": "ok", "use_case": use_case}


# ── System Prompts ────────────────────────────────────────────


@router.get("/prompts/{use_case}")
async def get_prompts(use_case: str, role: str = "default"):
    """Get the system prompt for a use case and role.

    Single-prompt endpoint kept for backward-compat with the older UI.
    """
    try:
        prompt = await get_system_prompt(use_case, role)
        return {"use_case": use_case, "role": role, "prompt": prompt}
    except ValueError as exc:
        return {"error": str(exc)}


class PromptUpdate(BaseModel):
    role: str = "default"
    prompt: str


@router.put("/prompts/{use_case}")
async def update_prompt(use_case: str, body: PromptUpdate):
    """Update the system prompt for a use case and role."""
    try:
        await set_system_prompt(use_case, body.role, body.prompt)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"status": "ok", "use_case": use_case, "role": body.role}


# ── Generic prompt-key API (multi-prompt editor) ─────────────


@router.get("/prompt-keys/{use_case}")
async def list_prompt_keys(use_case: str):
    """Return all editable prompt keys for a use case + their current values.

    Three groups:
    - ``agent.system.default``  — per-usecase agent system prompt (default
      shown is the file fallback so the user can see what they'd be overriding).
    - ``agent.react_suffix``    — global (scope='*'); same fallback display.
    - ``vision.alt_text``       — per-usecase; only the DB override is shown
      because the hardcoded default lives in the cleaning service.
    """
    overrides = await list_prompts(use_case)
    global_overrides = await list_prompts(GLOBAL_USE_CASE)
    vision_value = overrides.get("vision.alt_text", "")
    return {
        "use_case": use_case,
        "prompts": [
            {
                "key": "agent.system.default",
                "scope": use_case,
                "label": "Agent System Prompt",
                "value": await get_system_prompt(use_case, "default"),
                "is_override": "agent.system.default" in overrides,
            },
            {
                "key": PROMPT_KEY_REACT_SUFFIX,
                "scope": GLOBAL_USE_CASE,
                "label": "ReAct Suffix (global)",
                "value": await get_react_suffix(),
                "is_override": PROMPT_KEY_REACT_SUFFIX in global_overrides,
            },
            {
                "key": "vision.alt_text",
                "scope": use_case,
                "label": "Bild-Beschreibung (Vision)",
                "value": vision_value,
                "is_override": "vision.alt_text" in overrides,
            },
        ],
    }


class PromptKeyUpdate(BaseModel):
    scope: str  # use_case id, or '*' for global keys
    key: str
    content: str  # empty string clears the override


@router.put("/prompt-keys")
async def update_prompt_key(body: PromptKeyUpdate):
    """Upsert any (scope, prompt_key) pair. Empty content clears the override."""
    if body.key == PROMPT_KEY_REACT_SUFFIX:
        await set_react_suffix(body.content)
    else:
        await upsert_prompt(body.scope, body.key, body.content)
    return {"status": "ok", "scope": body.scope, "key": body.key}


# ── Per-usecase Config (LLM provider + models + secrets) ─────


def _redact(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


@router.get("/config/{use_case}")
async def get_config(use_case: str):
    """Return effective config for a use case.

    API-key fields are never returned in plaintext — only a "set" flag and
    a redacted preview of the last 4 chars (so admins can verify they
    didn't paste in the wrong key).
    """
    cfg = await resolve_config(use_case)
    return {
        "use_case": use_case,
        "crypto_configured": is_configured(),
        "config": {
            "chat_provider": cfg.chat_provider,
            "embedding_provider": cfg.embedding_provider,
            "vision_provider": cfg.vision_provider,
            "ollama_base_url": cfg.ollama_base_url,
            "openai_base_url": cfg.openai_base_url,
            "ollama_api_key_set": bool(cfg.ollama_api_key),
            "ollama_api_key_preview": _redact(cfg.ollama_api_key),
            "openai_api_key_set": bool(cfg.openai_api_key),
            "openai_api_key_preview": _redact(cfg.openai_api_key),
            "llm_model": cfg.llm_model,
            "embedding_model": cfg.embedding_model,
            "vision_model": cfg.vision_model,
            "embedding_dimension": cfg.embedding_dimension,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "embed_batch_size": cfg.embed_batch_size,
            "agent_max_steps": cfg.agent_max_steps,
            "memory_max_chars": cfg.memory_max_chars,
        },
    }


class ConfigUpdate(BaseModel):
    """Partial update. Only fields explicitly set are written.

    For API keys, send ``ollama_api_key`` / ``openai_api_key`` plaintext —
    the server encrypts before storing. Send empty string to clear; send
    ``None`` (omit the field) to leave unchanged.
    """

    chat_provider: str | None = None
    embedding_provider: str | None = None
    vision_provider: str | None = None
    ollama_base_url: str | None = None
    openai_base_url: str | None = None
    ollama_api_key: str | None = None
    openai_api_key: str | None = None
    llm_model: str | None = None
    embedding_model: str | None = None
    vision_model: str | None = None
    embedding_dimension: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    embed_batch_size: int | None = None
    agent_max_steps: int | None = None
    memory_max_chars: int | None = None


@router.put("/config/{use_case}")
async def update_config(use_case: str, body: ConfigUpdate):
    fields: dict = body.model_dump(exclude_unset=True)

    # Encrypt API keys before persisting; map plaintext field names to
    # *_encrypted columns. Empty string → clear the column.
    for plain_col, enc_col in (
        ("ollama_api_key", "ollama_api_key_encrypted"),
        ("openai_api_key", "openai_api_key_encrypted"),
    ):
        if plain_col in fields:
            value = fields.pop(plain_col)
            if value == "":
                fields[enc_col] = ""
            else:
                try:
                    fields[enc_col] = encrypt(value)
                except CryptoError as exc:
                    return {"error": "crypto_unavailable", "detail": str(exc)}

    try:
        await upsert_config(use_case, fields)
    except RuntimeError as exc:
        return {"error": "db_unavailable", "detail": str(exc)}
    return {"status": "ok", "use_case": use_case}


class ConfigTestRequest(BaseModel):
    """Optional unsaved form values. Empty/omitted fields keep the stored config."""

    chat_provider: str | None = None
    vision_provider: str | None = None
    ollama_base_url: str | None = None
    openai_base_url: str | None = None
    ollama_api_key: str | None = None
    openai_api_key: str | None = None
    llm_model: str | None = None
    vision_model: str | None = None


@router.post("/config/{use_case}/test")
async def test_config(use_case: str, body: ConfigTestRequest):
    """Probe chat (and vision) against the effective use-case config.

    Unsaved form values in the body overlay the stored config so admins can
    verify a new URL or key before saving. Failures come back as German
    ``message`` + ``hint`` rather than raw provider text.
    """
    cfg = overlay_config(await resolve_config(use_case), body.model_dump(exclude_unset=True))
    result = await run_connection_tests(cfg)
    result["use_case"] = use_case
    return result


# ── Available models on the configured chat provider ─────────


@router.get("/models")
async def list_available_models(use_case: str = ""):
    """Return model names from the chat provider (env or per-use-case config)."""
    cfg = await resolve_config(use_case) if use_case else LLMConfig.from_env()
    return await list_chat_models(cfg)


# ── Skills / Regeln ──────────────────────────────────────────


def _skill_to_dict(s: Skill) -> dict:
    return {
        "id": s.id,
        "use_case": s.use_case,
        "name": s.name,
        "overview": s.overview,
        "detailed_task": s.detailed_task,
        "enabled": s.enabled,
        "position": s.position,
        # Plugin layer. The source itself is fetched separately and the
        # variable *values* are never part of any skill listing.
        "code_language": s.code_language,
        "code_file_name": s.code_file_name,
        "has_code": s.has_code,
        "variable_count": s.variable_count,
    }


@router.get("/skills/{use_case}")
async def get_skills(use_case: str):
    """List all skills for a use case (enabled and disabled, ordered).

    Each skill carries its function table. The overview has to answer "what can
    this actually do?" at a glance — which functions exist, which ask before
    they run, which hosts they reach — and fetching that per skill would mean
    one request per row before the page could say anything.
    """
    skills = await list_skills(use_case)
    by_skill: dict[str, list] = {}
    for fn in await list_skill_functions(use_case, only_enabled=False):
        by_skill.setdefault(fn.skill_id, []).append(fn)
    return {
        "use_case": use_case,
        "skills": [
            {**_skill_to_dict(s),
             "functions": [_function_to_dict(f) for f in by_skill.get(s.id, [])]}
            for s in skills
        ],
    }


class SkillCreate(BaseModel):
    name: str
    overview: str
    detailed_task: str
    enabled: bool = True
    position: int = 0


@router.post("/skills/{use_case}")
async def create_skill_endpoint(use_case: str, body: SkillCreate):
    try:
        skill = await create_skill(
            use_case,
            body.name,
            body.overview,
            body.detailed_task,
            enabled=body.enabled,
            position=body.position,
        )
    except RuntimeError as exc:
        return {"error": "db_unavailable", "detail": str(exc)}
    except Exception as exc:  # unique-constraint etc.
        return {"error": "create_failed", "detail": str(exc)}
    return {"status": "ok", "skill": _skill_to_dict(skill)}


class SkillUpdate(BaseModel):
    name: str | None = None
    overview: str | None = None
    detailed_task: str | None = None
    enabled: bool | None = None
    position: int | None = None


@router.put("/skills/{skill_id}")
async def update_skill_endpoint(skill_id: str, body: SkillUpdate):
    try:
        skill = await update_skill(
            skill_id,
            name=body.name,
            overview=body.overview,
            detailed_task=body.detailed_task,
            enabled=body.enabled,
            position=body.position,
        )
    except RuntimeError as exc:
        return {"error": "db_unavailable", "detail": str(exc)}
    if skill is None:
        return {"error": "not_found", "detail": f"Skill {skill_id} not found"}
    return {"status": "ok", "skill": _skill_to_dict(skill)}


@router.delete("/skills/{skill_id}")
async def delete_skill_endpoint(skill_id: str):
    try:
        ok = await delete_skill(skill_id)
    except RuntimeError as exc:
        return {"error": "db_unavailable", "detail": str(exc)}
    if not ok:
        return {"error": "not_found", "detail": f"Skill {skill_id} not found"}
    return {"status": "ok"}


# ── Skill-Funktionen (Plugin-Layer) ──────────────────────────
#
# Everything below sits under /v1/admin, which the frontend gate restricts to
# role=admin. That gate is the access control for skill variables: their
# values are reachable only here and in the execution step, and never through
# the skill listing an ordinary user can load.


def _function_to_dict(f: SkillFunction) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "signature": f.signature,
        "description": f.description,
        "params": f.params,
        # The effective flag, plus what it was derived from — the UI shows a
        # hint when an admin's choice differs from the source file.
        "ask_for_permission": f.ask_for_permission,
        "permission_from_code": f.permission_from_code,
        "permission_override": f.permission_override,
        "allowed_domains": f.allowed_domains,
        "timeout_ms": f.timeout_ms,
        "memory_mb": f.memory_mb,
    }


@router.get("/skills/item/{skill_id}/code")
async def get_skill_code_endpoint(skill_id: str):
    """A skill's code file plus the function table derived from it."""
    skill = await get_skill(skill_id)
    if skill is None:
        return {"error": "not_found", "detail": f"Skill {skill_id} not found"}
    code = await get_skill_code(skill_id)
    functions = await list_functions_for_skill(skill_id)
    return {
        "language": code[0] if code else None,
        "file_name": code[1] if code else None,
        "source": code[2] if code else "",
        "functions": [_function_to_dict(f) for f in functions],
    }


class SkillCodeSave(BaseModel):
    file_name: str = Field(
        description="Decides the language: .py → Python, .js → JavaScript"
    )
    source: str


@router.put("/skills/item/{skill_id}/code")
async def put_skill_code(skill_id: str, body: SkillCodeSave):
    """Store a skill's code file and re-derive its callable functions.

    This is the import step. The source is parsed here, once, and what it
    declares — names, parameters, ``ask for permission``, ``allow-network`` —
    is written to the database. The execution path reads those rows and never
    re-reads the source, so the flag cannot change between check and run.
    """
    skill = await get_skill(skill_id)
    if skill is None:
        return {"error": "not_found", "detail": f"Skill {skill_id} not found"}

    try:
        language = detect_language(body.file_name)
        parsed = parse_skill_code(body.source, language)
    except SkillCodeError as exc:
        return {"error": "invalid_code", "detail": str(exc)}

    functions = [
        {
            "name": f.name,
            "signature": f.signature(),
            "description": f.description,
            "params": list(f.params),
            "ask_for_permission": f.ask_for_permission,
            "allowed_domains": list(f.allowed_domains),
        }
        for f in parsed
    ]
    try:
        await set_skill_code(
            skill_id,
            language=language,
            file_name=body.file_name,
            source=body.source,
            functions=functions,
        )
    except RuntimeError as exc:
        return {"error": "db_unavailable", "detail": str(exc)}
    except LookupError as exc:
        return {"error": "not_found", "detail": str(exc)}

    return {
        "status": "ok",
        "language": language,
        "functions": functions,
        "permission_required_count": sum(
            1 for f in functions if f["ask_for_permission"]
        ),
    }


@router.delete("/skills/item/{skill_id}/code")
async def delete_skill_code_endpoint(skill_id: str):
    """Remove a skill's code file; it reverts to a pure prompt-text skill."""
    try:
        ok = await clear_skill_code(skill_id)
    except RuntimeError as exc:
        return {"error": "db_unavailable", "detail": str(exc)}
    if not ok:
        return {"error": "not_found", "detail": f"Skill {skill_id} not found"}
    return {"status": "ok"}


class FunctionPermission(BaseModel):
    ask: bool | None = Field(
        description="True = vor Ausführung fragen, False = nicht fragen, "
                    "null = wieder der Markierung im Quelltext folgen."
    )


@router.put("/skills/item/{skill_id}/functions/{function_name}/permission")
async def put_function_permission(
    skill_id: str, function_name: str, body: FunctionPermission
):
    """Turn the consent prompt for one function on or off.

    Stored separately from the flag derived at import time, so saving the code
    file again does not quietly undo it. Sending ``null`` hands the decision
    back to the ``ask for permission`` marker in the source.
    """
    try:
        ok = await set_function_permission(skill_id, function_name, body.ask)
    except RuntimeError as exc:
        return {"error": "unavailable", "detail": str(exc)}
    if not ok:
        return {"error": "not_found",
                "detail": f"Funktion {function_name!r} gehört nicht zu diesem Skill."}
    return {"status": "ok", "function": function_name, "ask": body.ask}


@router.get("/skills/item/{skill_id}/variables")
async def get_skill_variables_endpoint(skill_id: str):
    """Variable *names* and a masked preview — never the values.

    An API key that has been stored should not be readable back out through
    the UI that stored it. Editing a value means overwriting it, which is the
    same contract the per-use-case LLM API keys already use.
    """
    skill = await get_skill(skill_id)
    if skill is None:
        return {"error": "not_found", "detail": f"Skill {skill_id} not found"}
    values = await get_skill_variables(skill_id)
    return {
        "skill_id": skill_id,
        "variables": [
            {"name": name, "preview": _redact(str(value) if value is not None else "")}
            for name, value in sorted(values.items())
        ],
    }


class SkillVariablesSave(BaseModel):
    variables: dict[str, str | None] = Field(
        default_factory=dict,
        description="The complete variable object. A null value means 'keep "
                    "what is stored' — the UI never receives a value, so that "
                    "is the only way it can leave one untouched. Names absent "
                    "from the object are deleted.",
    )


#: Names must be usable as an environment variable and as an identifier in
#: both runtimes, because the runner injects them as both.
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_VARIABLES = 64
_MAX_VARIABLE_LENGTH = 4096


@router.put("/skills/item/{skill_id}/variables")
async def put_skill_variables(skill_id: str, body: SkillVariablesSave):
    """Write a skill's variable object.

    The endpoint replaces, but a ``null`` value carries the stored value
    forward. That matters because the read side is masked: the UI never holds
    the real value of an existing key, so without this an admin who edits one
    variable would overwrite every other one with its own mask.
    """
    if len(body.variables) > _MAX_VARIABLES:
        return {"error": "too_many", "detail": f"Maximal {_MAX_VARIABLES} Variablen."}

    existing = await get_skill_variables(skill_id)
    merged: dict[str, str] = {}
    for name, value in body.variables.items():
        if not _VARIABLE_NAME_RE.match(name):
            return {
                "error": "invalid_name",
                "detail": f"Ungültiger Variablenname {name!r} — erlaubt sind "
                          "Buchstaben, Ziffern und Unterstrich (nicht mit einer "
                          "Ziffer beginnend).",
            }
        if value is None:
            if name not in existing:
                return {
                    "error": "unknown_variable",
                    "detail": f"Variable {name!r} soll unverändert bleiben, "
                              "existiert aber nicht.",
                }
            merged[name] = str(existing[name])
            continue
        if len(value) > _MAX_VARIABLE_LENGTH:
            return {
                "error": "too_long",
                "detail": f"Wert von {name!r} überschreitet "
                          f"{_MAX_VARIABLE_LENGTH} Zeichen.",
            }
        merged[name] = value

    try:
        ok = await set_skill_variables(skill_id, merged)
    except RuntimeError as exc:
        return {"error": "db_unavailable", "detail": str(exc)}
    if not ok:
        return {"error": "not_found", "detail": f"Skill {skill_id} not found"}
    return {"status": "ok", "count": len(merged)}


# ── Export / Import ──────────────────────────────────────────


@router.get("/skills/{use_case}/export")
async def export_skills(use_case: str, skill_id: str | None = None):
    """Skills of a use case as one portable JSON document.

    With ``skill_id`` the document holds exactly that one skill — same shape,
    so a single skill and a whole use case import through the same path.

    Carries the code file, because a skill without it is only half a skill.
    Carries variable **names** but never their values: an export is a file that
    lands in someone's download folder, gets mailed around and committed to a
    repository. A key in there is a leaked key. The names travel so the import
    side can say which values still have to be filled in.
    """
    skills = await list_skills(use_case)
    if skill_id:
        skills = [s for s in skills if s.id == skill_id]
        if not skills:
            return {"error": "not_found",
                    "detail": f"Skill {skill_id} gehört nicht zu {use_case}."}
    by_skill: dict[str, list] = {}
    for fn in await list_skill_functions(use_case, only_enabled=False):
        by_skill.setdefault(fn.skill_id, []).append(fn)

    exported: list[dict] = []
    for skill in skills:
        entry: dict = {
            "name": skill.name,
            "overview": skill.overview,
            "detailed_task": skill.detailed_task,
            "enabled": skill.enabled,
            "position": skill.position,
        }
        code = await get_skill_code(skill.id) if skill.has_code else None
        if code:
            entry["code_language"] = code[0]
            entry["code_file_name"] = code[1]
            entry["code"] = code[2]
            entry["functions"] = [
                {"name": f.name, "signature": f.signature,
                 # Effective flag, plus the admin's choice when one was made.
                 "ask_for_permission": f.ask_for_permission,
                 "permission_override": f.permission_override,
                 "allowed_domains": f.allowed_domains}
                for f in by_skill.get(skill.id, [])
            ]
        if skill.variable_count:
            names = sorted(await get_skill_variables(skill.id))
            entry["variable_names"] = names
        exported.append(entry)

    return {
        "version": 1,
        "use_case": use_case,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "note": "Variablenwerte sind absichtlich nicht enthalten.",
        "skills": exported,
    }


class SkillImport(BaseModel):
    version: int = 1
    skills: list[dict] = Field(default_factory=list)
    overwrite: bool = Field(
        default=False,
        description="Bestehende Skills gleichen Namens ersetzen statt überspringen.",
    )


@router.post("/skills/{use_case}/import")
async def import_skills(use_case: str, body: SkillImport):
    """Create or update skills from an exported document.

    Importing is importing *code that this platform will execute*, so the
    result reports exactly what arrived — per skill, which functions now exist,
    which of them ask before running, which hosts they may reach, and which
    variables still have no value. An admin should not have to go looking for
    that after the fact.

    A skill whose name already exists is skipped unless ``overwrite`` is set;
    that keeps a re-import from silently discarding local edits.
    """
    if body.version != 1:
        return {"error": "unsupported_version",
                "detail": f"Version {body.version} wird nicht unterstützt."}

    existing = {s.name: s for s in await list_skills(use_case)}
    created: list[dict] = []
    updated: list[dict] = []
    skipped: list[str] = []
    failed: list[dict] = []

    for entry in body.skills:
        name = (entry.get("name") or "").strip()
        if not name:
            failed.append({"name": "(ohne Namen)", "error": "Kein Name angegeben."})
            continue

        skill = existing.get(name)
        if skill is not None and not body.overwrite:
            skipped.append(name)
            continue

        source = entry.get("code") or ""
        file_name = entry.get("code_file_name") or ""
        parsed = None
        if source:
            # Parse before writing anything: an unreadable file must not leave
            # a half-imported skill behind.
            try:
                language = detect_language(file_name or "skill.py")
                parsed = parse_skill_code(source, language)
            except SkillCodeError as exc:
                failed.append({"name": name, "error": str(exc)})
                continue

        try:
            if skill is None:
                skill = await create_skill(
                    use_case, name,
                    entry.get("overview", ""), entry.get("detailed_task", ""),
                    enabled=bool(entry.get("enabled", True)),
                    position=int(entry.get("position", 0)),
                )
                target = created
            else:
                await update_skill(
                    skill.id,
                    overview=entry.get("overview"),
                    detailed_task=entry.get("detailed_task"),
                    enabled=entry.get("enabled"),
                    position=entry.get("position"),
                )
                target = updated

            functions: list[dict] = []
            if parsed:
                functions = [
                    {"name": f.name, "signature": f.signature(),
                     "description": f.description, "params": list(f.params),
                     "ask_for_permission": f.ask_for_permission,
                     "allowed_domains": list(f.allowed_domains)}
                    for f in parsed
                ]
                await set_skill_code(
                    skill.id, language=language, file_name=file_name,
                    source=source, functions=functions,
                )

                # A consent override is not in the source, so it has to travel
                # in the document — but only in one direction. Adopting
                # ``true`` re-applies a gate someone deliberately switched on.
                # Adopting ``false`` would let a hand-edited file *remove* a
                # gate the source file asks for, which is exactly the thing a
                # signed-off skill must not be able to lose in transit.
                declared = {
                    f.get("name"): f.get("permission_override")
                    for f in (entry.get("functions") or [])
                    if isinstance(f, dict)
                }
                for fn in functions:
                    if declared.get(fn["name"]) is True and not fn["ask_for_permission"]:
                        await set_function_permission(skill.id, fn["name"], True)

            elif skill.has_code and body.overwrite:
                # The import says this skill has no code; honour that rather
                # than leaving the previous file silently in place.
                await clear_skill_code(skill.id)

            # Report the *effective* state, read back from the database rather
            # than from the parse above. An override carried across the rewrite
            # is invisible in the parsed list, so reporting that list told an
            # admin "asks for nothing" while a consent gate was in fact active
            # — wrong exactly where the report is supposed to be trustworthy.
            effective = await list_functions_for_skill(skill.id)
            stored = await get_skill_variables(skill.id)
            target.append({
                "name": name,
                "language": entry.get("code_language") or (
                    detect_language(file_name) if file_name else None),
                "functions": [f.signature for f in effective],
                "needs_permission": [
                    f.signature for f in effective if f.ask_for_permission
                ],
                "reaches_hosts": sorted({
                    d for f in effective for d in f.allowed_domains
                }),
                "missing_variables": [
                    n for n in (entry.get("variable_names") or []) if not stored.get(n)
                ],
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skill import failed for %r: %s", name, exc)
            failed.append({"name": name, "error": str(exc)[:300]})

    return {
        "status": "ok",
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
    }


@router.get("/skills/{use_case}/executions")
async def get_skill_executions(use_case: str, limit: int = 50):
    """Audit trail: recent skill-function executions for a use case."""
    try:
        rows = await list_skill_executions(use_case, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": "read_failed", "detail": str(exc), "executions": []}
    return {"use_case": use_case, "executions": rows}


# ── Agent Actions ────────────────────────────────────────────


@router.get("/actions/catalog")
async def get_action_catalog():
    """Return the global action catalog with descriptions."""
    return {"actions": ACTION_CATALOG}


@router.get("/actions/{use_case}")
async def get_actions(use_case: str):
    """Get all actions for a use case with enabled/disabled state and descriptions."""
    try:
        actions = get_agent_actions_full(use_case)
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "use_case": use_case,
        "actions": {
            name: {"enabled": defn["enabled"], "description": defn["description"]}
            for name, defn in actions.items()
        },
    }


class ActionUpdate(BaseModel):
    enabled: bool | None = None
    description: str | None = None


@router.put("/actions/{use_case}/{action_name}")
async def update_action(use_case: str, action_name: str, body: ActionUpdate):
    """Update enabled state and/or description of an action."""
    try:
        if body.enabled is not None:
            set_action_enabled(use_case, action_name, body.enabled)
        if body.description is not None:
            set_action_description(use_case, action_name, body.description)
    except ValueError as exc:
        return {"error": str(exc)}
    actions = get_agent_actions_full(use_case)
    return {
        "status": "ok",
        "use_case": use_case,
        "action": action_name,
        **actions[action_name],
    }


# ── Documents / Ingestion Log ────────────────────────────────


@router.get("/documents")
async def list_documents(use_case: str = ""):
    """List ingested documents."""
    pool = await get_pool()
    if use_case:
        rows = await pool.fetch(
            """SELECT id, file_name, file_hash, stored_path, collection,
                      use_case, chunk_count, status, created_at
               FROM ingestion_log
               WHERE use_case = $1
               ORDER BY created_at DESC""",
            use_case,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, file_name, file_hash, stored_path, collection,
                      use_case, chunk_count, status, created_at
               FROM ingestion_log
               ORDER BY created_at DESC"""
        )
    return {
        "documents": [
            {
                "id": str(r["id"]),
                "file_name": r["file_name"],
                "file_hash": r["file_hash"],
                "stored_path": r.get("stored_path", ""),
                "collection": r["collection"],
                "use_case": r["use_case"],
                "chunk_count": r["chunk_count"],
                "status": r["status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.delete("/documents/{doc_id}")
async def delete_document_log(doc_id: str):
    """Remove one ingestion_log row and return the row's payload so the
    orchestrator can chase the file_hash through Qdrant + cleaning."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT id, file_name, file_hash, stored_path, collection,
                  use_case, chunk_count, status
           FROM ingestion_log WHERE id = $1""",
        doc_id,
    )
    if row is None:
        return {"error": "not_found", "detail": f"Document {doc_id} not found"}
    await pool.execute("DELETE FROM ingestion_log WHERE id = $1", doc_id)
    return {
        "status": "ok",
        "document": {
            "id": str(row["id"]),
            "file_name": row["file_name"],
            "file_hash": row["file_hash"],
            "stored_path": row.get("stored_path", ""),
            "collection": row["collection"],
            "use_case": row["use_case"],
        },
    }


# ── Use-Case Registry (editable + exportable) ──────────────────

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class UseCaseInput(BaseModel):
    slug: str
    label: str
    description: str = ""
    color: str = ""
    accent: str = ""
    roles: list[str] = ["default"]
    agent_action_names: list[str] = []
    default_collection: str
    collection_prefixes: list[str] = []
    enabled: bool = True
    prompts: dict[str, str] = {}


def _system_prompt_key(role: str) -> str:
    return f"agent.system.{role}"


async def _serialize_use_case(uc) -> dict:
    """Use case in the config-JSON shape (incl. system prompts)."""
    db_prompts = await list_prompts(uc.id)
    prompts = {
        role: db_prompts[_system_prompt_key(role)]
        for role in uc.roles
        if _system_prompt_key(role) in db_prompts
    }
    return {
        "id": uc.id,
        "slug": uc.slug,
        "label": uc.label,
        "description": uc.description,
        "color": uc.color,
        "accent": uc.accent,
        "enabled": uc.enabled,
        "roles": uc.roles,
        "agent_action_names": uc.agent_action_names,
        "default_collection": uc.default_collection,
        "collection_prefixes": uc.collection_prefixes,
        "prompts": prompts,
    }


@router.get("/use-cases")
async def admin_list_use_cases():
    """Full use-case list (incl. disabled) in the config-JSON shape.

    The frontend uses this both to edit and to build the exportable JSON.
    """
    cases = await list_use_cases()
    return {"version": 1, "use_cases": [await _serialize_use_case(uc) for uc in cases]}


def collection_outside_prefixes(
    use_case_id: str, default_collection: str, prefixes: list[str]
) -> str | None:
    """Why ``default_collection`` would be invisible to the agent – or ``None``.

    Search is hard-isolated per use case: only collections starting with one of
    its prefixes are searched (no prefixes → ``<id>_``). A default collection
    outside them still receives every upload, but the agent may never read it –
    the documents look uploaded and are never found.
    """
    effective = prefixes or [f"{use_case_id}_"]
    if any(default_collection.startswith(p) for p in effective):
        return None
    return (
        f"Default-Collection „{default_collection}“ passt zu keinem Collection-Präfix "
        f"({', '.join(effective)}). Hochgeladene Dokumente würden dort gespeichert, "
        "aber von der Suche nie gefunden – Collection oder Präfix anpassen."
    )


@router.put("/use-cases/{use_case_id}")
async def admin_upsert_use_case(use_case_id: str, body: UseCaseInput):
    """Create or update a use case (and its system prompts), then hot-reload."""
    if not _ID_RE.match(use_case_id):
        raise HTTPException(
            status_code=400,
            detail="id must match ^[a-z][a-z0-9_]*$ (lowercase, digits, underscore)",
        )
    if problem := collection_outside_prefixes(
        use_case_id, body.default_collection, body.collection_prefixes
    ):
        raise HTTPException(status_code=400, detail=problem)
    await upsert_use_case(
        use_case_id,
        slug=body.slug,
        label=body.label,
        description=body.description,
        color=body.color,
        accent=body.accent,
        roles=body.roles,
        agent_action_names=body.agent_action_names,
        default_collection=body.default_collection,
        collection_prefixes=body.collection_prefixes,
        enabled=body.enabled,
    )
    for role, content in body.prompts.items():
        await upsert_prompt(use_case_id, _system_prompt_key(role), content)
    await refresh_use_cases()
    uc = await get_use_case(use_case_id)
    return await _serialize_use_case(uc)


@router.delete("/use-cases/{use_case_id}")
async def admin_delete_use_case(use_case_id: str):
    """Delete a use case, then hot-reload the in-memory registry."""
    removed = await delete_use_case(use_case_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Use case not found")
    await refresh_use_cases()
    return {"status": "ok", "deleted": use_case_id}


# ── Chunk Utilization Analysis ─────────────────────────────────


class ChunkAnalysisRequest(BaseModel):
    """Request body for claim-level chunk utilization analysis."""
    threshold: float = 0.70
    max_concurrent: int = 8
    models: list[str] = ["qwen3.5:9b"]
    mapping_model: str | None = None
    nli_model: str = "deberta"



@router.get("/analyze-chunks/{query_id}/progress")
async def get_analysis_progress(query_id: str):
    from core.chunk_analyzer import _analysis_progress
    progress = _analysis_progress.get(query_id, {"done": 0, "total": 0})
    return progress

@router.get("/analyze-chunks/{query_id}/result")
async def get_analysis_result(query_id: str):
    from core.chunk_analyzer import _analysis_results
    result = _analysis_results.get(query_id)
    if result is None:
        return {"status": "not_found"}
    if result["status"] == "done":
        _analysis_results.pop(query_id, None)
    return result

@router.post("/analyze-chunks/{query_id}")
async def analyze_chunks(query_id: str, body: ChunkAnalysisRequest = ChunkAnalysisRequest()):
    """Run claim-level chunk utilization analysis for a completed query.

    LLM extracts atomic claims via local Ollama → embed → cosine.
    Does NOT modify the stored query — purely read + compute.
    """
    from core.chunk_analyzer import analyze_chunk_utilization_claim

    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT id, use_case, query_text, answer_text, agent_steps
           FROM queries WHERE id = $1""",
        uuid.UUID(query_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Query not found")

    use_case = row["use_case"]
    agent_steps = json.loads(row["agent_steps"]) if row["agent_steps"] else []
    answer_text = row["answer_text"] or ""

    from core.chunk_analyzer import _analysis_progress

    # Guard against double-clicks
    if query_id in _analysis_progress:
        return {"status": "already_running"}

    # Initialize progress before spawning background task
    _analysis_progress[query_id] = {"done": 0, "total": 0}

    async def _run_analysis():
        from core.chunk_analyzer import _analysis_results
        try:
            results = []
            for step_i, step in enumerate(agent_steps):
                chunks = step.get("chunks") or []
                if not chunks:
                    continue
                action = step.get("action", "")

                step_analyses: dict[str, dict] = {}
                # Model 1: Generator — full claim extraction + matching
                gen_model = body.models[0]
                gen_analysis = await analyze_chunk_utilization_claim(
                    chunks=chunks,
                    answer_text=answer_text,
                    threshold=body.threshold,
                    model=gen_model,
                    max_concurrent=body.max_concurrent,
                    query_id=query_id,
                    mapping_model=body.mapping_model,
                    nli_model=body.nli_model,
                )
                step_analyses[gen_model] = gen_analysis

                results.append({
                    "step_index": step_i,
                    "step_label": f"Schritt {step_i + 1}" + (f": {action}" if action else ""),
                    "action": action,
                    "chunk_count": len(chunks),
                    "analyses": step_analyses,
                })
            _analysis_results[query_id] = {
                "status": "done",
                "data": {
                    "query_id": query_id,
                    "query_text": row["query_text"],
                    "answer_text": answer_text,
                    "threshold": body.threshold,
                    "mode": "claim",
                    "models": body.models,
                    "steps": results,
                },
            }
        except Exception as exc:
            logger.exception(f"Analysis failed for query {query_id}")
            error_msg = str(exc) or type(exc).__name__
            _analysis_results[query_id] = {"status": "error", "error": error_msg}
        finally:
            _analysis_progress.pop(query_id, None)

    asyncio.create_task(_run_analysis())
    return {"status": "started"}


# ── Metrics Dashboard ────────────────────────────────────────

@router.get("/metrics")
async def get_metrics(use_case: str = "", limit: int = 50):
    """Return queries with computed metrics for the evaluation dashboard."""
    pool = await get_pool()
    if use_case:
        rows = await pool.fetch(
            """SELECT id, use_case, session_id, role, query_text, answer_text,
                      agent_steps, citations, images, sufficient, scores, created_at
               FROM queries
               WHERE use_case = $1
               ORDER BY created_at DESC
               LIMIT $2""",
            use_case, limit,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, use_case, session_id, role, query_text, answer_text,
                      agent_steps, citations, images, sufficient, scores, created_at
               FROM queries
               ORDER BY created_at DESC
               LIMIT $1""",
            limit,
        )

    items = []
    for r in rows:
        agent_steps = json.loads(r["agent_steps"]) if r["agent_steps"] else []
        citations = json.loads(r["citations"]) if r["citations"] else []
        scores = json.loads(r["scores"]) if r["scores"] else {}

        # Count total chunks across all steps
        total_chunks = sum(len(s.get("chunks", [])) for s in agent_steps)

        # Count sub-tasks from subagent_ids in steps
        subagent_ids = set()

        # New: total tokens and best retrieval quality
        total_tokens = 0
        has_tokens = False
        max_rq = -1.0
        has_rq = False

        for s in agent_steps:
            sid = s.get("subagent_id")
            if sid:
                subagent_ids.add(sid)

            # Tokens
            llm = s.get("llm_timing") or {}
            pt = llm.get("prompt_tokens")
            ct = llm.get("completion_tokens")
            if pt is not None or ct is not None:
                has_tokens = True
                total_tokens += int(pt or 0) + int(ct or 0)

            # Retrieval quality: best rerank_score across all chunks
            for c in s.get("chunks", []):
                sq = c.get("rerank_score") or c.get("score")
                if sq is not None:
                    has_rq = True
                    if sq > max_rq:
                        max_rq = sq

        # Extract compliance data from agent steps
        compliance_verdict = None
        compliance_issues_count = 0
        compliance_category_counts: dict[str, int] = {
            "citation_coverage": 0, "source_authenticity": 0,
            "hallucination": 0, "use_case_policy": 0, "unknown": 0,
        }
        for s in agent_steps:
            if s.get("action") == "COMPLIANCE_CHECK":
                args = s.get("args", {})
                if isinstance(args, str):
                    args = json.loads(args)
                if isinstance(args, dict):
                    compliance_verdict = args.get("verdict")
                    compliance_issues_count = len(args.get("issues", []))
                    classified = args.get("classified_issues", [])
                    for ci in classified:
                        if isinstance(ci, dict):
                            cat = ci.get("category", "unknown")
                            compliance_category_counts[cat] = compliance_category_counts.get(cat, 0) + 1
                    if not classified and compliance_issues_count > 0:
                        compliance_category_counts["unknown"] += compliance_issues_count
                break
        # Fallback: check scores for older queries
        if not compliance_verdict:
            compliance_verdict = scores.get("compliance_verdict")

        items.append({
            "id": str(r["id"]),
            "use_case": r["use_case"],
            "session_id": str(r["session_id"]) if r["session_id"] else None,
            "role": r["role"],
            "query_text": r["query_text"],
            "answer_text": (r["answer_text"] or ""),  # full text
            "answer_length": len(r["answer_text"] or ""),
            "step_count": len(agent_steps),
            "subtask_count": len(subagent_ids) or 1,
            "chunk_count": total_chunks,
            "citation_count": len(citations),
            "sufficient": r["sufficient"],
            "processing_ms": scores.get("processing_ms"),
            "compliance_verdict": compliance_verdict,
            "compliance_issues_count": compliance_issues_count,
            "compliance_category_counts": compliance_category_counts,
            "merge_strategy": scores.get("merge_strategy"),
            "model": scores.get("model"),
            "llm_provider": scores.get("llm_provider"),
            "total_tokens": total_tokens if has_tokens else None,
            "retrieval_quality": max_rq if has_rq else None,
            "agent_steps": agent_steps,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    # Compliance totals
    compliance_total = sum(1 for it in items if it.get("compliance_verdict"))
    compliance_ok = sum(1 for it in items if it.get("compliance_verdict") == "OK")
    compliance_rewrite = sum(1 for it in items if it.get("compliance_verdict") == "REWRITE")
    compliance_refuse = sum(1 for it in items if it.get("compliance_verdict") == "REFUSE")
    compliance_total_issues = sum(it.get("compliance_issues_count", 0) for it in items)
    compliance_category_totals: dict[str, int] = {}
    for it in items:
        for cat, count in it.get("compliance_category_counts", {}).items():
            compliance_category_totals[cat] = compliance_category_totals.get(cat, 0) + count

    # Summary stats
    if items:
        avg_steps = sum(it["step_count"] for it in items) / len(items)
        avg_chunks = sum(it["chunk_count"] for it in items) / len(items)
        avg_answer_len = sum(it["answer_length"] for it in items) / len(items)
        sufficient_count = sum(1 for it in items if it["sufficient"])
        avg_processing = sum(it["processing_ms"] or 0 for it in items) / max(1, sum(1 for it in items if it["processing_ms"]))
        token_items = [it["total_tokens"] for it in items if it["total_tokens"] is not None]
        avg_tokens = sum(token_items) / len(token_items) if token_items else None
        rq_items = [it["retrieval_quality"] for it in items if it["retrieval_quality"] is not None]
        avg_rq = sum(rq_items) / len(rq_items) if rq_items else None
    else:
        avg_steps = avg_chunks = avg_answer_len = avg_processing = 0
        sufficient_count = 0
        avg_tokens = avg_rq = None

    return {
        "queries": items,
        "summary": {
            "total": len(items),
            "avg_steps": round(avg_steps, 1),
            "avg_chunks": round(avg_chunks, 1),
            "avg_answer_length": round(avg_answer_len, 0),
            "sufficient_count": sufficient_count,
            "sufficient_pct": round(sufficient_count / max(1, len(items)) * 100, 1),
            "avg_processing_ms": round(avg_processing, 0) if avg_processing else None,
            "avg_total_tokens": round(avg_tokens, 0) if avg_tokens else None,
            "avg_retrieval_quality": round(avg_rq, 3) if avg_rq else None,
            "compliance_total": compliance_total,
            "compliance_ok": compliance_ok,
            "compliance_rewrite": compliance_rewrite,
            "compliance_refuse": compliance_refuse,
            "compliance_total_issues": compliance_total_issues,
            "compliance_category_totals": compliance_category_totals,
        },
    }


# ── API Tokens (public pipeline API) ──────────────────────────


class CreateApiTokenRequest(BaseModel):
    name: str
    # Empty list = the token may use every use case.
    use_cases: list[str] = Field(default_factory=list)
    expires_in_days: int | None = None


def _token_out(token: ApiToken) -> dict:
    return {
        "id": token.id,
        "name": token.name,
        "token_prefix": token.token_prefix,
        "use_cases": token.use_cases,
        "created_by": token.created_by,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "expires_at": token.expires_at,
        "revoked_at": token.revoked_at,
        "active": token.active,
    }


@router.get("/api-tokens")
async def get_api_tokens():
    """List API tokens. Secrets are never returned — only the display prefix."""
    return {"tokens": [_token_out(t) for t in await list_api_tokens()]}


@router.post("/api-tokens", status_code=201)
async def post_api_token(body: CreateApiTokenRequest, created_by: str = ""):
    """Create a token.

    The plaintext is in the response and nowhere else, ever again — only its
    hash is stored, so a lost token has to be replaced rather than looked up.
    """
    try:
        token, plaintext = await create_api_token(
            body.name,
            use_cases=body.use_cases,
            created_by=created_by or None,
            expires_in_days=body.expires_in_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**_token_out(token), "token": plaintext}


@router.post("/api-tokens/{token_id}/revoke")
async def revoke_api_token_endpoint(token_id: str):
    """Disable a token without losing the record of it having existed."""
    if not await revoke_api_token(token_id):
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    return {"status": "ok", "id": token_id}


@router.delete("/api-tokens/{token_id}", status_code=204)
async def delete_api_token_endpoint(token_id: str):
    """Delete a token permanently. Its past jobs keep working (token_id → NULL)."""
    if not await delete_api_token(token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return None


@router.get("/api-jobs")
async def list_api_jobs(use_case: str = "", limit: int = 50):
    """Recent public-API requests — the operational view for the dashboard."""
    pool = await get_pool()
    limit = max(1, min(limit, 500))
    if use_case:
        rows = await pool.fetch(
            """SELECT j.id, j.use_case, j.session_id, j.status, j.query_text,
                      j.error, j.created_at, j.started_at, j.finished_at,
                      t.name AS token_name
               FROM api_jobs j LEFT JOIN api_tokens t ON t.id = j.token_id
               WHERE j.use_case = $1
               ORDER BY j.created_at DESC LIMIT $2""",
            use_case,
            limit,
        )
    else:
        rows = await pool.fetch(
            """SELECT j.id, j.use_case, j.session_id, j.status, j.query_text,
                      j.error, j.created_at, j.started_at, j.finished_at,
                      t.name AS token_name
               FROM api_jobs j LEFT JOIN api_tokens t ON t.id = j.token_id
               ORDER BY j.created_at DESC LIMIT $1""",
            limit,
        )
    return {
        "jobs": [
            {
                "request_id": str(r["id"]),
                "use_case": r["use_case"],
                "session_id": str(r["session_id"]),
                "status": r["status"],
                "query": r["query_text"],
                "error": r["error"],
                "token_name": r["token_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            }
            for r in rows
        ]
    }

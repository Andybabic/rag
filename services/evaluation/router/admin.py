"""Admin API – query history, memory, prompts, per-usecase config."""

from __future__ import annotations

import json

from core.database import get_pool
from core.memory import read_memory, write_memory
from core.prompts import ACTION_CATALOG
from core.use_cases import (
    GLOBAL_USE_CASE,
    PROMPT_KEY_REACT_SUFFIX,
    get_agent_actions_full,
    get_react_suffix,
    get_system_prompt,
    set_action_description,
    set_action_enabled,
    set_react_suffix,
    set_system_prompt,
)
from fastapi import APIRouter
from pydantic import BaseModel
from shared.security.crypto import CryptoError, encrypt, is_configured
from shared.usecase_config import (
    Skill,
    create_skill,
    delete_skill,
    list_prompts,
    list_skills,
    resolve_config,
    update_skill,
    upsert_config,
    upsert_prompt,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ── Query History ─────────────────────────────────────────────


@router.get("/queries")
async def list_queries(use_case: str = "", limit: int = 50, offset: int = 0):
    """List past queries, optionally filtered by use_case."""
    pool = await get_pool()
    if use_case:
        rows = await pool.fetch(
            """SELECT id, use_case, session_id, role, query_text, answer_text,
                      agent_steps, sufficient, created_at
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
                      agent_steps, sufficient, created_at
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
                "sufficient": r["sufficient"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
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
    }


@router.get("/skills/{use_case}")
async def get_skills(use_case: str):
    """List all skills for a use case (enabled and disabled, ordered)."""
    skills = await list_skills(use_case)
    return {"use_case": use_case, "skills": [_skill_to_dict(s) for s in skills]}


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

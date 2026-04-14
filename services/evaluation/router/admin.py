"""Admin API – query history, memory, system prompts."""

from __future__ import annotations

import json

from core.database import get_pool
from core.memory import read_memory, write_memory
from core.prompts import ACTION_CATALOG
from core.use_cases import (
    get_agent_actions_full,
    get_system_prompt,
    set_action_description,
    set_action_enabled,
    set_system_prompt,
)
from fastapi import APIRouter
from pydantic import BaseModel

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
    """Get the system prompt for a use case and role."""
    try:
        prompt = get_system_prompt(use_case, role)
        return {"use_case": use_case, "role": role, "prompt": prompt}
    except ValueError as exc:
        return {"error": str(exc)}


class PromptUpdate(BaseModel):
    role: str = "default"
    prompt: str


@router.put("/prompts/{use_case}")
async def update_prompt(use_case: str, body: PromptUpdate):
    """Update the system prompt for a use case and role."""
    set_system_prompt(use_case, body.role, body.prompt)
    return {"status": "ok", "use_case": use_case, "role": body.role}


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

"""Admin API – query history, memory, prompts, per-usecase config."""


from __future__ import annotations
import asyncio
import json
import logging
import re
import uuid

from core.database import get_pool
from core.memory import read_memory, write_memory
from core.prompts import ACTION_CATALOG
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
from pydantic import BaseModel
from shared.llm.config import LLMConfig
from shared.security.crypto import CryptoError, encrypt, is_configured
from shared.usecase_config import (
    Skill,
    create_skill,
    delete_skill,
    delete_use_case,
    get_use_case,
    list_prompts,
    list_skills,
    list_use_cases,
    resolve_config,
    update_skill,
    upsert_config,
    upsert_prompt,
    upsert_use_case,
)

import logging

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


# ── Available Ollama models ──────────────────────────────────


@router.get("/models")
async def list_ollama_models():
    """Return model names currently available on the llama.cpp server."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://172.19.0.1:8081/v1/models")
            data = r.json()
            models = [m["id"] for m in data.get("data", [])]
            models.sort()
            return {"models": models}
    except Exception as exc:
        return {"models": ["qwen3.5:9b", "gemma4:12b"], "error": f"llama.cpp nicht erreichbar: {exc}"}


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


@router.put("/use-cases/{use_case_id}")
async def admin_upsert_use_case(use_case_id: str, body: UseCaseInput):
    """Create or update a use case (and its system prompts), then hot-reload."""
    if not _ID_RE.match(use_case_id):
        raise HTTPException(
            status_code=400,
            detail="id must match ^[a-z][a-z0-9_]*$ (lowercase, digits, underscore)",
        )
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

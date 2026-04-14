from __future__ import annotations

from core.chunker import chunk_markdown
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from models import CNCStructureRequest, StructureRequest
from plugins import PLUGIN_REGISTRY, get_plugin

router = APIRouter(prefix="/v1", tags=["v1"])


@router.post("/structure")
async def structure(body: StructureRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")

    if body.use_case not in PLUGIN_REGISTRY:
        available = list(PLUGIN_REGISTRY.keys())
        return JSONResponse(
            status_code=400,
            content={
                "error": "unknown_use_case",
                "detail": f"Use case {body.use_case!r} not found. "
                f"Available: {', '.join(available)}",
                "request_id": request_id,
                "service": "data-structure-service",
            },
        )

    plugin = get_plugin(body.use_case)
    collections = plugin.get_collections()
    collection = body.config.target_collection or collections[0]

    chunks = chunk_markdown(
        body.markdown,
        file_name=body.metadata.get("file_name", "unknown"),
        doc_type=body.metadata.get("doc_type", "unknown"),
        use_case=body.use_case,
        collection=collection,
        total_pages=body.metadata.get("total_pages"),
        extra=body.config.extra,
        chunk_size=body.config.chunk_size,
        chunk_overlap=body.config.chunk_overlap,
        plugin=plugin,
    )

    return {
        "status": "ok",
        "request_id": request_id,
        "use_case": body.use_case,
        "total_chunks": len(chunks),
        "routing": {"collection": collection},
        "chunks": [c.model_dump() for c in chunks],
    }


@router.post("/structure/cnc")
async def structure_cnc(body: CNCStructureRequest, request: Request):
    """Explicit CNC structuring – splits into cnc_blocks, ruest_chunks, material_chunks."""
    request_id = getattr(request.state, "request_id", "unknown")

    plugin = get_plugin("gw_stpoelten")
    collection = body.config.target_collection or plugin.get_collections()[0]

    chunks = chunk_markdown(
        body.markdown,
        file_name=body.metadata.get("file_name", "unknown"),
        doc_type=body.metadata.get("doc_type", "unknown"),
        use_case="gw_stpoelten",
        collection=collection,
        total_pages=body.metadata.get("total_pages"),
        extra={
            **body.config.extra,
            "product_id": body.metadata.get("product_id"),
            "ruest_map_id": body.metadata.get("ruest_map_id"),
        },
        chunk_size=body.config.chunk_size,
        chunk_overlap=body.config.chunk_overlap,
        plugin=plugin,
    )

    cnc_blocks = []
    ruest_chunks = []
    material_chunks = []

    for c in chunks:
        dump = c.model_dump()
        if c.metadata.extra.get("is_cnc_block"):
            cnc_blocks.append({
                "cnc_step_id": c.id,
                "operation_type": c.metadata.extra.get("operation_type", "unbekannt"),
                "cutting_speed": c.metadata.extra.get("cutting_speed"),
                "tool_type": c.metadata.extra.get("tool_type"),
                "material_class": c.metadata.extra.get("material_class"),
                "text": c.text,
                "metadata": dump["metadata"],
            })
        elif c.metadata.collection == "gw_material_info":
            material_chunks.append(dump)
        else:
            ruest_chunks.append(dump)

    return {
        "status": "ok",
        "request_id": request_id,
        "cnc_blocks": cnc_blocks,
        "ruest_chunks": ruest_chunks,
        "material_chunks": material_chunks,
    }


@router.get("/use-cases")
async def use_cases():
    """Return all registered use cases and their metadata."""
    return {
        name: {
            "default_collection": plugin.get_collections()[0],
            "collections": plugin.get_collections(),
            "description": plugin.__class__.__doc__ or "",
            "agent_actions": plugin.get_agent_actions(),
        }
        for name, plugin in PLUGIN_REGISTRY.items()
    }

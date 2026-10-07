"""Admin API – export a use case to an archive and import it elsewhere.

Flow for an import: create an upload, send the archive in chunks (each below
the frontend's body limit), inspect it (manifest + whether the id is taken),
then start the import under the original or a new id. Export and import run as
background jobs; the dashboard polls ``/jobs/{id}``.

Admin-only through the frontend (``/api/admin`` in hooks.server.ts).
"""

from __future__ import annotations

from core import transfer
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter(prefix="/v1/admin/transfer", tags=["admin"])


def _bad(exc: transfer.TransferError, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/export/{use_case}")
async def export_use_case(use_case: str):
    from shared.usecase_config import get_use_case

    if await get_use_case(use_case) is None:
        raise HTTPException(status_code=404, detail=f"Use Case „{use_case}“ existiert nicht.")
    return transfer.start_export(use_case).public()


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = transfer.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job unbekannt oder abgelaufen.")
    return job.public()


@router.get("/jobs/{job_id}/download")
async def job_download(job_id: str):
    job = transfer.get_job(job_id)
    if job is None or job.kind != "export" or job.status != "done" or job.file is None:
        raise HTTPException(status_code=404, detail="Kein fertiger Export unter dieser ID.")
    if not job.file.exists():
        raise HTTPException(status_code=410, detail="Die Export-Datei ist abgelaufen.")
    return FileResponse(
        job.file,
        media_type="application/zip",
        filename=(job.result or {}).get("file_name", f"{job.use_case}.zip"),
    )


@router.post("/uploads")
async def create_upload():
    return {"upload_id": transfer.new_upload()}


@router.put("/uploads/{upload_id}")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0):
    try:
        size = transfer.append_upload(upload_id, offset, await request.body())
    except transfer.TransferError as exc:
        raise _bad(exc, 409) from exc
    return {"upload_id": upload_id, "size": size}


@router.delete("/uploads/{upload_id}")
async def delete_upload(upload_id: str):
    try:
        transfer.discard_upload(upload_id)
    except transfer.TransferError as exc:
        raise _bad(exc) from exc
    return {"status": "ok"}


class InspectRequest(BaseModel):
    target_id: str | None = None


@router.post("/uploads/{upload_id}/inspect")
async def inspect_upload(upload_id: str, body: InspectRequest | None = None):
    """What the archive holds, and what an import under ``target_id`` (default:
    the original id) would run into."""
    try:
        manifest = transfer.read_manifest(upload_id)
        target = (body.target_id if body and body.target_id else None) or manifest["use_case"]["id"]
        _, plan = await transfer.prepare_import(upload_id, target, None)
    except transfer.TransferError as exc:
        raise _bad(exc) from exc
    state = await transfer.target_state(target)
    return {
        "manifest": manifest,
        "target_id": target,
        "exists": manifest["use_case"]["id"] in state.use_cases,
        "slug": plan.slug,
        "collections": plan.rename.collections,
        "problems": plan.problems,
        "warnings": plan.warnings,
    }


class ImportRequest(BaseModel):
    target_id: str
    label: str | None = None


@router.post("/uploads/{upload_id}/import")
async def import_upload(upload_id: str, body: ImportRequest):
    try:
        manifest, plan = await transfer.prepare_import(upload_id, body.target_id.strip(), body.label)
    except transfer.TransferError as exc:
        raise _bad(exc) from exc
    if plan.problems:
        raise HTTPException(
            status_code=409,
            detail={"problems": plan.problems, "warnings": plan.warnings},
        )
    return transfer.start_import(upload_id, manifest, plan).public()

from __future__ import annotations

import base64
import binascii
import io
import zipfile

from core.chunker import chunk_markdown
from core.cnc_parser import build_cnc_chunks, decode_cnc_bytes, parse_cnc
from core.folder_ingest import (
    ProductGroup,
    derive_bearbeitung,
    group_folder,
    is_noncanonical_path,
    plan_folder,
    program_role,
    relative_to_product,
)
from core.stueckliste import build_material_chunk, extract_material_class
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from models import (
    CNCStructureRequest,
    FolderPlanRequest,
    ProductFilesRequest,
    StructureRequest,
)
from plugins import PLUGIN_REGISTRY, resolve_plugin

router = APIRouter(prefix="/v1", tags=["v1"])


@router.post("/structure")
async def structure(body: StructureRequest, request: Request):
    request_id = getattr(request.state, "request_id", "unknown")

    # Built-in plugins first; a use case created in the dashboard has no code
    # of its own and is looked up in the database instead.
    plugin = await resolve_plugin(body.use_case)
    if plugin is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "unknown_use_case",
                "detail": f"Use case {body.use_case!r} is neither built in "
                f"({', '.join(PLUGIN_REGISTRY)}) nor defined in the dashboard.",
                "request_id": request_id,
                "service": "data-structure-service",
            },
        )

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
        images=body.images,
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
    """CNC structuring – split a Sinumerik/MPF program into one chunk per
    tool operation (collection ``gw_cnc_steps``).

    ``body.markdown`` is the decoded CNC text from the cleaning service.
    The parser handles both dialects, normalises garbled umlauts and
    classifies each operation into a canonical ``operation_type``.
    ``metadata.material_class`` (from the product's Stückliste) is woven
    into each operation's embedding text when available.
    """
    request_id = getattr(request.state, "request_id", "unknown")

    prog = parse_cnc(body.markdown)
    chunks = build_cnc_chunks(
        prog,
        file_name=body.metadata.get("file_name", prog.program_name or "unknown"),
        material_class=body.metadata.get("material_class"),
        extra=body.config.extra,
    )

    return {
        "status": "ok",
        "request_id": request_id,
        "use_case": "gw_stpoelten",
        "product_id": prog.product_id,
        "bearbeitung": prog.bearbeitung,
        "dialect": prog.dialect,
        "total_chunks": len(chunks),
        "routing": {"collection": "gw_cnc_steps"},
        "chunks": [c.model_dump() for c in chunks],
    }


@router.post("/structure/folder")
async def structure_folder(request: Request):
    """Folder ingest for GW St. Pölten – accepts a ZIP of one or more
    product folders in the raw request body.

    Per product the files are grouped and routed:
      * CNC programs (often extension-less) → one chunk per tool operation
        in ``gw_cnc_steps``, with the product's ``material_class`` woven in
      * Stückliste (.csv) → the material class + a ``gw_material_info`` chunk
      * Einstellblatt (.pdf) / images are detected and reported, but their
        text extraction runs through the existing clean→structure path
        (they need the cleaning service); they are NOT chunked here.

    Templates (``*VORLAGE*`` / ``Programm-Vorlagen-ALT/``) are skipped.
    Returns CNC + material chunks ready for embedding/upsert.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    body = await request.body()
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        return JSONResponse(
            status_code=400,
            content={
                "error": "bad_zip",
                "detail": "Request body is not a valid ZIP archive.",
                "request_id": request_id,
                "service": "data-structure-service",
            },
        )

    files: list[tuple[str, bytes]] = [
        (info.filename, zf.read(info))
        for info in zf.infolist()
        if not info.is_dir()
    ]
    groups = group_folder(files)

    # Transparenz: was wurde als nicht-kanonisch übersprungen (Vorlagen,
    # CAM-Arbeitsordner) – damit Skips sichtbar sind, nicht stillschweigend.
    skipped = sorted(
        p for p, _ in files
        if is_noncanonical_path(p) and not p.rsplit("/", 1)[-1].startswith("~$")
    )

    all_chunks, products = _structure_groups(groups)
    return {
        "status": "ok",
        "request_id": request_id,
        "use_case": "gw_stpoelten",
        "products": products,
        "total_chunks": len(all_chunks),
        "skipped_noncanonical": skipped,
        "chunks": [c.model_dump() for c in all_chunks],
    }


def _structure_groups(groups: list[ProductGroup]) -> tuple[list, list[dict]]:
    """CNC- und Material-Chunks für gruppierte Produkte bauen.

    Gemeinsamer Kern von ZIP-Upload und Produkt-Upload. Jeder Chunk trägt
    Maschine, Kategorie und Programm-Rolle: ohne sie ließen sich weder die
    Programme desselben Bauteils auf zwei Maschinen auseinanderhalten, noch
    ein Produkt beim erneuten Hochladen gezielt ersetzen.
    """
    all_chunks = []
    products = []
    for g in groups:
        location = {"machine": g.machine, "category": g.category}
        material_class = None
        material_chunks = []
        if g.stueckliste is not None:
            name, raw = g.stueckliste
            csv_text = decode_cnc_bytes(raw)
            material_class = extract_material_class(csv_text)
            mc = build_material_chunk(
                csv_text, file_name=name.rsplit("/", 1)[-1],
                product_id=g.product_id, extra=location,
            )
            if mc is not None:
                material_chunks.append(mc)

        cnc_chunks = []
        programs = []
        for rel_path, raw in g.cnc:
            prog = parse_cnc(decode_cnc_bytes(raw))
            # Der Ordner ist die verlässlichere Produkt-ID: Kopfzeilen fehlen
            # oder verweisen auf ein Nachbarteil („11120.4225" in 11221.4225).
            prog.product_id = g.product_id
            role = program_role(rel_path)
            chunks = build_cnc_chunks(
                prog,
                file_name=rel_path.rsplit("/", 1)[-1],
                material_class=material_class,
                extra={
                    **location,
                    "bearbeitung": prog.bearbeitung or derive_bearbeitung(rel_path),
                    "program_role": role,
                    "source_path": relative_to_product(rel_path),
                },
            )
            cnc_chunks.extend(chunks)
            programs.append({
                "file": relative_to_product(rel_path),
                "operations": len(chunks),
                "dialect": prog.dialect,
                "role": role,
            })

        all_chunks.extend(cnc_chunks)
        all_chunks.extend(material_chunks)
        products.append({
            "product_id": g.product_id,
            **location,
            "cnc_files": len(g.cnc),
            "operations": len(cnc_chunks),
            "material_class": material_class,
            "programs": programs,
            "einstellblaetter": [p.rsplit("/", 1)[-1] for p, _ in g.einstellblatt],
            "images": len(g.images),
            # Ein Programm ohne erkannte Operation ist fast immer ein neuer
            # Dialekt, den der Parser nicht kennt – sichtbar machen statt
            # stillschweigend nichts zu indexieren.
            "warnings": [
                f"Keine Operation erkannt: {p['file']}"
                for p in programs if p["operations"] == 0
            ],
        })
    return all_chunks, products


@router.post("/structure/plan")
async def structure_plan(body: FolderPlanRequest):
    """Upload-Plan für einen GW-Datenbestand – nur anhand der Pfade.

    Der Browser schickt die Pfadliste des gewählten Ordners und erfährt, welche
    Produkte (je Maschine) darin liegen und welche Dateien er dafür hochladen
    muss. Fotos, ``.doc``-Fassungen, Vorlagen und CAM-Ordner verlassen den
    Rechner des Nutzers damit gar nicht erst.
    """
    return plan_folder(body.paths)


@router.post("/structure/product")
async def structure_product(body: ProductFilesRequest, request: Request):
    """Strukturiere die Dateien eines Produkts (CNC-Programme + Stückliste).

    Nimmt die Dateien als base64 mit ihrem relativen Pfad entgegen – der Pfad
    trägt Maschine und Kategorie. Produkte ohne CNC-Programm werden trotzdem
    verarbeitet, damit ihre Stückliste nicht verloren geht.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        files = [(f.path, base64.b64decode(f.content_b64, validate=True)) for f in body.files]
    except (binascii.Error, ValueError):
        return JSONResponse(
            status_code=400,
            content={
                "error": "bad_encoding",
                "detail": "content_b64 is not valid base64.",
                "request_id": request_id,
                "service": "data-structure-service",
            },
        )
    groups = group_folder(files, require_cnc=False)
    if body.machine:
        for g in groups:
            g.machine = body.machine
            g.category = body.category or g.category
    all_chunks, products = _structure_groups(groups)
    return {
        "status": "ok",
        "request_id": request_id,
        "use_case": "gw_stpoelten",
        "products": products,
        "total_chunks": len(all_chunks),
        "chunks": [c.model_dump() for c in all_chunks],
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

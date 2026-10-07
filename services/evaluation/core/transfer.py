"""Move a whole use case between instances: export to one archive, import it.

A use case is spread over three stores, and an archive has to carry all of
them to be worth anything on the other side:

* **Postgres** — the use case itself, prompts, config, skills (with code and
  functions), agent memory, the document list, chat history, feedback,
  evaluation runs, skill audit trail, user assignments.
* **Qdrant** — every collection behind the use case's prefixes plus its memory
  collection ``mem_<id>``, *with vectors*: an import copies them 1:1 instead of
  re-embedding, which is why the target must use the same embedding model.
* **File storage** (cleaning service) — originals, extracted images, sidecars
  under ``<storage>/<use_case>/``.

Archive layout (ZIP64)::

    manifest.json              what is inside, counts, what was left out
    db.json                    table -> list of rows
    vectors/<collection>.jsonl one point per line: id, v (base64 float32), payload
    files/<path>               relative to the use case's storage folder

Secrets never travel: API keys of the use-case config and the *values* of
skill variables are dropped at export; the variable names stay so the target
can say what still has to be filled in.

Import under another id (required when the id is taken on the target) renames
everything that carries the id: rows, collections (``<new>_…``), the prefix,
stored paths, and references inside JSON columns and vector payloads. All row
ids are re-issued on every import, so a clone never collides with its source.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from config import settings
from core.database import get_pool
from shared.llm.config import LLMConfig

logger = logging.getLogger(__name__)

FORMAT = "kermit-usecase-export"
FORMAT_VERSION = 1
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_VECTOR_PAGE = 256
_FILE_CHUNK = 1 << 20
# Actions that search collections by a fixed name instead of the use case's
# prefixes (core/actions.py). A renamed clone keeps them, but they still read
# the *original* collections.
FIXED_COLLECTION_ACTIONS = {"SEARCH_CNC", "SEARCH_TOOL"}

# Tables in insert order (parents before children). Each is exported with
# every column; the import writes the columns the target schema knows.
_TABLES = [
    "use_cases",
    "usecase_config",
    "usecase_prompts",
    "usecase_skills",
    "usecase_skill_functions",
    "agent_memory",
    "ingestion_log",
    "queries",
    "feedback",
    "evaluation_runs",
    "skill_executions",
]
# Columns whose content can mention collections, stored paths or the id.
_REWRITE_TEXT_COLUMNS = {"collection", "stored_path"}
_SECRET_CONFIG_COLUMNS = {"ollama_api_key_encrypted", "openai_api_key_encrypted"}


class TransferError(Exception):
    """An export or import that cannot proceed; the message is user-facing."""


# ── Renaming ────────────────────────────────────────────────────────────────


@dataclass
class Rename:
    """Everything that changes when a use case arrives under another id.

    With ``old == new`` every mapping is the identity, so one code path serves
    both a plain move and a clone.
    """

    old: str
    new: str
    collections: dict[str, str] = field(default_factory=dict)

    @property
    def renamed(self) -> bool:
        return self.old != self.new

    def collection(self, name: str) -> str:
        if not self.renamed:
            return name
        if name == f"mem_{self.old}":
            return f"mem_{self.new}"
        if name.startswith(f"{self.old}_"):
            return self.new + name[len(self.old):]
        return f"{self.new}_{name}"

    def prefixes(self, prefixes: list[str]) -> list[str]:
        # Every renamed collection starts with "<new>_", so that one prefix
        # finds all of them — and nothing of the use case it was cloned from.
        return [f"{self.new}_"] if self.renamed else list(prefixes)

    def value(self, v: Any) -> Any:
        """Rewrite references inside a JSON value (payload, JSONB column)."""
        if not self.renamed:
            return v
        if isinstance(v, dict):
            return {
                k: (self.new if k == "use_case" and x == self.old else self.value(x))
                for k, x in v.items()
            }
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, str):
            return self.text(v)
        return v

    def text(self, s: str) -> str:
        if s in self.collections:
            return self.collections[s]
        if s.startswith(f"{self.old}/"):
            # stored_path: "<use_case>/<hash[:2]>/<file>"
            s = f"{self.new}/{s[len(self.old) + 1:]}"
        for route in ("/api/images/", "/api/documents/"):
            s = s.replace(f"{route}{self.old}/", f"{route}{self.new}/")
        return s


# ── Vectors ─────────────────────────────────────────────────────────────────


def encode_vector(vector: list[float]) -> str:
    """float32 little-endian, base64. Qdrant stores float32, so this is exact
    and a third of the size of the same vector as JSON numbers."""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def decode_vector(data: str) -> list[float]:
    raw = base64.b64decode(data)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _model_key(name: str | None) -> str:
    """Ollama treats "bge-m3" and "bge-m3:latest" as the same model."""
    return (name or "").removesuffix(":latest")


# ── Rows <-> JSON ───────────────────────────────────────────────────────────


def _jsonable(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


async def _column_types(conn, table: str) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {r["column_name"]: r["data_type"] for r in rows}


def _row_to_json(record, types: dict[str, str]) -> dict:
    row = {}
    for key, value in dict(record).items():
        if types.get(key) == "jsonb" and isinstance(value, str):
            value = json.loads(value)
        row[key] = _jsonable(value)
    return row


async def dump_database(use_case: str) -> dict[str, list[dict]]:
    """Every row that belongs to ``use_case``, as JSON-ready dicts."""
    pool = await get_pool()
    queries = {
        "use_cases": ("SELECT * FROM use_cases WHERE id = $1", use_case),
        "usecase_config": ("SELECT * FROM usecase_config WHERE use_case = $1", use_case),
        "usecase_prompts": ("SELECT * FROM usecase_prompts WHERE use_case = $1", use_case),
        "usecase_skills": ("SELECT * FROM usecase_skills WHERE use_case = $1", use_case),
        "usecase_skill_functions": (
            "SELECT f.* FROM usecase_skill_functions f "
            "JOIN usecase_skills s ON s.id = f.skill_id WHERE s.use_case = $1",
            use_case,
        ),
        "agent_memory": ("SELECT * FROM agent_memory WHERE use_case = $1", use_case),
        "ingestion_log": ("SELECT * FROM ingestion_log WHERE use_case = $1", use_case),
        "queries": ("SELECT * FROM queries WHERE use_case = $1", use_case),
        "feedback": (
            "SELECT f.* FROM feedback f JOIN queries q ON q.id = f.query_id "
            "WHERE q.use_case = $1",
            use_case,
        ),
        "evaluation_runs": (
            "SELECT e.* FROM evaluation_runs e JOIN queries q ON q.id = e.query_id "
            "WHERE q.use_case = $1",
            use_case,
        ),
        "skill_executions": ("SELECT * FROM skill_executions WHERE use_case = $1", use_case),
    }
    out: dict[str, list[dict]] = {}
    async with pool.acquire() as conn:
        for table in _TABLES:
            sql, arg = queries[table]
            types = await _column_types(conn, table)
            out[table] = [_row_to_json(r, types) for r in await conn.fetch(sql, arg)]
        users = await conn.fetch(
            "SELECT u.username FROM user_use_cases a JOIN users u ON u.id = a.user_id "
            "WHERE a.use_case = $1 ORDER BY u.username",
            use_case,
        )
    out["user_use_cases"] = [{"username": r["username"]} for r in users]
    return out


def strip_secrets(db: dict[str, list[dict]]) -> dict[str, Any]:
    """Drop API keys and variable values in place; report what was dropped."""
    config_keys: list[str] = []
    for row in db.get("usecase_config", []):
        for col in sorted(_SECRET_CONFIG_COLUMNS):
            if row.get(col):
                config_keys.append(col.removesuffix("_encrypted"))
            row[col] = None
    skill_variables: dict[str, list[str]] = {}
    for row in db.get("usecase_skills", []):
        names = sorted((row.get("variables") or {}).keys())
        if names:
            skill_variables[row["name"]] = names
        # Names stay, values go: the target lists them as "still to fill in".
        row["variables"] = {n: "" for n in names}
    return {"config_keys": config_keys, "skill_variables": skill_variables}


# ── Jobs ────────────────────────────────────────────────────────────────────


@dataclass
class Job:
    id: str
    kind: str  # export | import
    use_case: str
    status: str = "running"  # running | done | error
    phase: str = "Start"
    done: int = 0
    total: int = 0
    result: dict | None = None
    error: str | None = None
    file: Path | None = None
    started: float = field(default_factory=time.time)

    def step(self, phase: str, total: int = 0) -> None:
        self.phase, self.done, self.total = phase, 0, total

    def public(self) -> dict:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "use_case": self.use_case,
            "status": self.status,
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "result": self.result,
            "error": self.error,
        }


_jobs: dict[str, Job] = {}
_KEEP_SECONDS = 24 * 3600


def transfer_dir() -> Path:
    path = Path(settings.TRANSFER_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_stale() -> None:
    """Forget jobs and delete archives/uploads older than a day."""
    cutoff = time.time() - _KEEP_SECONDS
    for job_id in [j.id for j in _jobs.values() if j.started < cutoff and j.status != "running"]:
        _jobs.pop(job_id, None)
    for path in transfer_dir().iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def _start(job: Job, coro) -> Job:
    _jobs[job.id] = job

    async def runner():
        try:
            job.result = await coro
            job.status = "done"
            job.phase = "Fertig"
        except TransferError as exc:
            job.status, job.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 — surfaced to the admin, logged here
            logger.exception("Transfer job %s failed", job.id)
            job.status, job.error = "error", f"{type(exc).__name__}: {exc}"

    asyncio.create_task(runner())
    return job


# ── Service calls ───────────────────────────────────────────────────────────


def _client(timeout: float = 120.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def effective_prefixes(use_case: str, prefixes: list[str] | None) -> list[str]:
    return list(prefixes or []) or [f"{use_case}_"]


async def list_collections() -> list[dict]:
    async with _client(30.0) as client:
        resp = await client.get(f"{settings.VECTORDB_SERVICE_URL}/v1/collections")
        resp.raise_for_status()
        return resp.json().get("collections", [])


def collections_of(use_case: str, prefixes: list[str], names: list[str]) -> list[str]:
    """The collections a use case owns — same rule as search and reset."""
    memory = f"mem_{use_case}"
    return sorted(n for n in names if n == memory or any(n.startswith(p) for p in prefixes))


async def list_files(use_case: str) -> list[dict]:
    async with _client(60.0) as client:
        resp = await client.get(f"{settings.CLEANING_SERVICE_URL}/v1/transfer/{use_case}/files")
        resp.raise_for_status()
        return resp.json().get("files", [])


# ── Export ──────────────────────────────────────────────────────────────────


def start_export(use_case: str) -> Job:
    cleanup_stale()
    job = Job(id=uuid.uuid4().hex, kind="export", use_case=use_case)
    return _start(job, _export(job, use_case))


async def _export(job: Job, use_case: str) -> dict:
    job.step("Datenbank lesen")
    db = await dump_database(use_case)
    if not db["use_cases"]:
        raise TransferError(f"Use Case „{use_case}“ existiert nicht.")
    uc = db["use_cases"][0]
    omitted = strip_secrets(db)

    prefixes = effective_prefixes(use_case, uc.get("collection_prefixes"))
    collections = collections_of(
        use_case, prefixes, [c["name"] for c in await list_collections()]
    )
    files = await list_files(use_case)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = transfer_dir() / f"export-{job.id}.zip"
    job.file = path
    job.result = {"file_name": f"{use_case}-{stamp}.zip"}

    manifest_collections: list[dict] = []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr("db.json", json.dumps(db, ensure_ascii=False))

        async with _client() as client:
            for i, name in enumerate(collections, 1):
                job.step(f"Vektoren {i}/{len(collections)}: {name}")
                entry = await _export_collection(client, zf, name, job)
                manifest_collections.append(entry)

            job.step("Dateien", total=len(files))
            total_bytes = 0
            for f in files:
                await _export_file(client, zf, use_case, f["path"])
                total_bytes += int(f.get("size") or 0)
                job.done += 1

        manifest = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "use_case": {
                "id": use_case,
                "label": uc.get("label", use_case),
                "slug": uc.get("slug", use_case),
                "default_collection": uc.get("default_collection"),
                "collection_prefixes": uc.get("collection_prefixes") or [],
                "agent_action_names": uc.get("agent_action_names") or [],
            },
            "counts": {table: len(rows) for table, rows in db.items()},
            "collections": manifest_collections,
            "files": {"count": len(files), "bytes": total_bytes},
            "omitted": omitted,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        **job.result,
        "size": path.stat().st_size,
        "counts": manifest["counts"],
        "collections": len(manifest_collections),
        "points": sum(c["points"] for c in manifest_collections),
        "files": len(files),
        "omitted": omitted,
    }


async def _export_collection(client, zf: zipfile.ZipFile, name: str, job: Job) -> dict:
    offset = None
    count = 0
    dimension = 0
    models: set[str] = set()
    with zf.open(f"vectors/{name}.jsonl", "w", force_zip64=True) as out:
        while True:
            resp = await client.post(
                f"{settings.VECTORDB_SERVICE_URL}/v1/points/export",
                json={"collection": name, "offset": offset, "limit": _VECTOR_PAGE},
            )
            resp.raise_for_status()
            page = resp.json()
            dimension = page.get("dimension") or dimension
            for p in page["points"]:
                payload = p.get("payload") or {}
                if payload.get("_embed_model"):
                    models.add(payload["_embed_model"])
                line = {"id": p["id"], "v": encode_vector(p["vector"]), "payload": payload}
                out.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
                count += 1
            job.done = count
            offset = page.get("next")
            if offset is None:
                break
    return {"name": name, "points": count, "dimension": dimension,
            "embed_models": sorted(models)}


async def _export_file(client, zf: zipfile.ZipFile, use_case: str, rel: str) -> None:
    url = f"{settings.CLEANING_SERVICE_URL}/v1/transfer/{use_case}/file/{rel}"
    # Originals and images are compressed already; deflating them again only
    # costs time.
    info = zipfile.ZipInfo(f"files/{rel}", date_time=time.localtime()[:6])
    info.compress_type = zipfile.ZIP_STORED
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with zf.open(info, "w", force_zip64=True) as out:
            async for chunk in resp.aiter_bytes(_FILE_CHUNK):
                out.write(chunk)


# ── Upload (chunked, so no single request exceeds the frontend body limit) ──


def _upload_path(upload_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise TransferError("Ungültige Upload-ID.")
    return transfer_dir() / f"upload-{upload_id}.zip"


def new_upload() -> str:
    cleanup_stale()
    upload_id = uuid.uuid4().hex
    _upload_path(upload_id).write_bytes(b"")
    return upload_id


def append_upload(upload_id: str, offset: int, data: bytes) -> int:
    path = _upload_path(upload_id)
    if not path.exists():
        raise TransferError("Upload unbekannt oder abgelaufen.")
    size = path.stat().st_size
    if offset != size:
        # A retried chunk must not be appended twice.
        raise TransferError(f"Offset {offset} passt nicht zur Uploadgröße {size}.")
    with open(path, "ab") as fh:
        fh.write(data)
    return size + len(data)


def read_manifest(upload_id: str) -> dict:
    path = _upload_path(upload_id)
    if not path.exists():
        raise TransferError("Upload unbekannt oder abgelaufen.")
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            names = set(zf.namelist())
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise TransferError(f"Keine gültige Export-Datei: {exc}") from exc
    if manifest.get("format") != FORMAT:
        raise TransferError("Die Datei ist kein Kermit-Use-Case-Export.")
    if manifest.get("version") != FORMAT_VERSION:
        raise TransferError(
            f"Export-Version {manifest.get('version')} wird nicht unterstützt "
            f"(erwartet {FORMAT_VERSION})."
        )
    missing = {"db.json"} | {f"vectors/{c['name']}.jsonl" for c in manifest["collections"]}
    if missing - names:
        raise TransferError(f"Export unvollständig, es fehlt: {', '.join(sorted(missing - names))}")
    return manifest


def discard_upload(upload_id: str) -> None:
    _upload_path(upload_id).unlink(missing_ok=True)


# ── Import planning ─────────────────────────────────────────────────────────


@dataclass
class TargetState:
    """What already exists on this instance — everything a clash is checked against."""

    use_cases: dict[str, dict]  # id -> {"slug", "prefixes"}
    collections: set[str]
    files_present: bool
    embed_model: str | None
    embed_dimension: int | None
    users: set[str]


@dataclass
class ImportPlan:
    rename: Rename
    slug: str
    label: str
    prefixes: list[str]
    default_collection: str
    problems: list[str]
    warnings: list[str]


def plan_import(
    manifest: dict,
    db_config: dict | None,
    target_id: str,
    label: str | None,
    state: TargetState,
    *,
    users: list[str] | None = None,
) -> ImportPlan:
    """Decide names and find every clash — before anything is written."""
    src = manifest["use_case"]
    problems: list[str] = []
    warnings: list[str] = []

    if not ID_RE.match(target_id or ""):
        problems.append(
            f"Ungültige ID „{target_id}“ (erlaubt: a–z, 0–9, _ ; Beginn mit Buchstabe)."
        )
    if target_id in state.use_cases:
        problems.append(f"Use Case „{target_id}“ existiert bereits – bitte einen neuen Namen wählen.")

    rename = Rename(old=src["id"], new=target_id)
    rename.collections = {c["name"]: rename.collection(c["name"]) for c in manifest["collections"]}
    prefixes = rename.prefixes(src.get("collection_prefixes") or [])
    default_collection = rename.collection(src["default_collection"])

    taken_slugs = {v["slug"]: k for k, v in state.use_cases.items()}
    slug = src.get("slug") or target_id
    if rename.renamed or slug in taken_slugs:
        slug = target_id.replace("_", "-")
    if slug in taken_slugs:
        problems.append(f"Slug „{slug}“ ist schon an „{taken_slugs[slug]}“ vergeben.")

    new_names = set(rename.collections.values())
    for name in sorted(new_names & state.collections):
        problems.append(f"Collection „{name}“ existiert auf dieser Instanz bereits.")

    # Search is isolated by prefix. A new collection that matches another use
    # case's prefix would show up in *its* search, and an existing collection
    # that matches the new prefix would show up in this one.
    own = effective_prefixes(target_id, prefixes)
    for other, meta in sorted(state.use_cases.items()):
        if other == target_id:
            continue
        for p in effective_prefixes(other, meta.get("prefixes")):
            hits = sorted(n for n in new_names if n.startswith(p))
            if hits:
                problems.append(
                    f"Collection „{hits[0]}“ fiele unter den Präfix „{p}“ von Use Case "
                    f"„{other}“ – bitte einen anderen Namen wählen."
                )
    foreign = sorted(
        n for n in state.collections - new_names if any(n.startswith(p) for p in own)
    )
    if foreign:
        problems.append(
            f"Auf dieser Instanz liegen bereits Collections mit dem Präfix "
            f"„{'“, „'.join(own)}“ ({', '.join(foreign[:3])}) – bitte einen anderen Namen wählen."
        )

    if state.files_present:
        problems.append(
            f"Die Dateiablage für „{target_id}“ ist nicht leer (Reste eines früheren "
            f"Use Case?) – bitte einen anderen Namen wählen."
        )

    # Vectors are copied, not re-embedded: the target must embed queries with
    # the model the collections were built with.
    target_model = (db_config or {}).get("embedding_model") or state.embed_model
    target_dim = (db_config or {}).get("embedding_dimension") or state.embed_dimension
    for c in manifest["collections"]:
        models = {_model_key(m) for m in c.get("embed_models") or []}
        if target_model and models and models != {_model_key(target_model)}:
            problems.append(
                f"Collection „{c['name']}“ wurde mit dem Embedding-Modell "
                f"„{', '.join(sorted(c['embed_models']))}“ erstellt, diese Instanz nutzt "
                f"„{target_model}“. Die Vektoren wären nicht vergleichbar – Import abgebrochen."
            )
        if target_dim and c.get("dimension") and c["dimension"] != target_dim:
            problems.append(
                f"Collection „{c['name']}“ hat {c['dimension']} Dimensionen, "
                f"diese Instanz erwartet {target_dim}."
            )

    fixed = sorted(FIXED_COLLECTION_ACTIONS & set(src.get("agent_action_names") or []))
    if rename.renamed and fixed:
        warnings.append(
            f"Die Aktionen {', '.join(fixed)} suchen in fest benannten Collections und "
            f"greifen im Klon weiterhin auf die Daten des Original-Use-Case zu."
        )
    omitted = manifest.get("omitted") or {}
    if omitted.get("config_keys"):
        warnings.append(
            "API-Keys der Konfiguration wurden nicht übertragen: "
            + ", ".join(omitted["config_keys"]) + "."
        )
    for skill, names in sorted((omitted.get("skill_variables") or {}).items()):
        warnings.append(f"Skill „{skill}“: Variablen ohne Wert – {', '.join(names)}.")
    unknown = sorted(set(users or []) - state.users)
    if unknown:
        warnings.append(
            "Benutzer auf dieser Instanz unbekannt, Zuordnung entfällt: " + ", ".join(unknown) + "."
        )

    return ImportPlan(
        rename=rename,
        slug=slug,
        label=(label or "").strip() or src.get("label") or target_id,
        prefixes=prefixes,
        default_collection=default_collection,
        problems=problems,
        warnings=warnings,
    )


async def target_state(target_id: str) -> TargetState:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, slug, collection_prefixes FROM use_cases")
        users = await conn.fetch("SELECT username FROM users")
    cases = {}
    for r in rows:
        prefixes = r["collection_prefixes"]
        if isinstance(prefixes, str):
            prefixes = json.loads(prefixes)
        cases[r["id"]] = {"slug": r["slug"], "prefixes": prefixes or []}
    files_present = bool(ID_RE.match(target_id or "")) and bool(await list_files(target_id))
    env = LLMConfig.from_env()
    return TargetState(
        use_cases=cases,
        collections={c["name"] for c in await list_collections()},
        files_present=files_present,
        embed_model=env.embedding_model,
        embed_dimension=env.embedding_dimension,
        users={r["username"] for r in users},
    )


def _read_db(upload_id: str) -> dict[str, list[dict]]:
    with zipfile.ZipFile(_upload_path(upload_id)) as zf:
        return json.loads(zf.read("db.json"))


async def prepare_import(upload_id: str, target_id: str, label: str | None) -> tuple[dict, ImportPlan]:
    manifest = read_manifest(upload_id)
    db = _read_db(upload_id)
    config = (db.get("usecase_config") or [None])[0]
    users = [u["username"] for u in db.get("user_use_cases", [])]
    plan = plan_import(manifest, config, target_id, label, await target_state(target_id), users=users)
    return manifest, plan


# ── Import ──────────────────────────────────────────────────────────────────


def start_import(upload_id: str, manifest: dict, plan: ImportPlan) -> Job:
    job = Job(id=uuid.uuid4().hex, kind="import", use_case=plan.rename.new)
    return _start(job, _import(job, upload_id, manifest, plan))


async def _import(job: Job, upload_id: str, manifest: dict, plan: ImportPlan) -> dict:
    # Checked again: minutes may have passed since the admin saw the preview.
    _, fresh = await prepare_import(upload_id, plan.rename.new, plan.label)
    if fresh.problems:
        raise TransferError(" ".join(fresh.problems))

    path = _upload_path(upload_id)
    created: list[str] = []
    files_written = False
    try:
        with zipfile.ZipFile(path) as zf:
            async with _client() as client:
                for i, c in enumerate(manifest["collections"], 1):
                    target = plan.rename.collections[c["name"]]
                    job.step(f"Vektoren {i}/{len(manifest['collections'])}: {target}", c["points"])
                    await _import_collection(client, zf, c, target, plan.rename, job)
                    created.append(target)

                file_entries = [n for n in zf.namelist() if n.startswith("files/") and not n.endswith("/")]
                job.step("Dateien", total=len(file_entries))
                for name in file_entries:
                    files_written = True
                    await _import_file(client, zf, name, plan.rename.new)
                    job.done += 1

            job.step("Datenbank schreiben")
            db = json.loads(zf.read("db.json"))
        counts = await write_database(db, plan)
    except BaseException:
        await _rollback(created, plan.rename.new if files_written else None)
        raise

    from core.use_cases import refresh_use_cases
    from shared.usecase_config import invalidate, invalidate_use_cases

    invalidate(plan.rename.new)
    invalidate_use_cases()
    await refresh_use_cases()
    discard_upload(upload_id)
    return {
        "use_case": plan.rename.new,
        "slug": plan.slug,
        "renamed_from": plan.rename.old if plan.rename.renamed else None,
        "collections": {c["name"]: plan.rename.collections[c["name"]] for c in manifest["collections"]},
        "points": sum(c["points"] for c in manifest["collections"]),
        "files": manifest.get("files", {}).get("count", 0),
        "counts": counts,
        "warnings": plan.warnings,
    }


async def _import_collection(client, zf, entry: dict, target: str, rename: Rename, job: Job) -> None:
    url = f"{settings.VECTORDB_SERVICE_URL}/v1/points/import"
    dimension = entry.get("dimension") or 0
    create = True
    batch: list[dict] = []

    async def flush() -> None:
        nonlocal create, batch
        resp = await client.post(url, json={
            "collection": target, "dimension": dimension, "points": batch, "create": create,
        })
        if resp.status_code == 409:
            raise TransferError(f"Collection „{target}“ existiert bereits.")
        resp.raise_for_status()
        job.done += len(batch)
        create, batch = False, []

    with zf.open(f"vectors/{entry['name']}.jsonl") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            p = json.loads(raw)
            vector = decode_vector(p["v"])
            dimension = dimension or len(vector)
            batch.append({"id": p["id"], "vector": vector, "payload": rename.value(p.get("payload") or {})})
            if len(batch) >= _VECTOR_PAGE:
                await flush()
    if batch or create:
        # An empty collection is still created, so the use case finds it.
        await flush()


async def _import_file(client, zf, name: str, use_case: str) -> None:
    rel = name[len("files/"):]

    async def body():
        with zf.open(name) as fh:
            while chunk := fh.read(_FILE_CHUNK):
                yield chunk

    resp = await client.put(
        f"{settings.CLEANING_SERVICE_URL}/v1/transfer/{use_case}/file/{rel}", content=body()
    )
    resp.raise_for_status()


async def _rollback(collections: list[str], files_of: str | None) -> None:
    """Undo what an aborted import wrote outside the database. Both targets
    were verified absent/empty before the import started, so removing them
    cannot touch anything that existed before."""
    async with _client(60.0) as client:
        for name in collections:
            try:
                await client.delete(f"{settings.VECTORDB_SERVICE_URL}/v1/collection/{name}")
            except httpx.HTTPError:
                logger.warning("Rollback: Collection %s nicht gelöscht", name)
        if files_of:
            try:
                await client.delete(f"{settings.CLEANING_SERVICE_URL}/v1/transfer/{files_of}")
            except httpx.HTTPError:
                logger.warning("Rollback: Dateien von %s nicht gelöscht", files_of)


def transform_rows(db: dict[str, list[dict]], plan: ImportPlan) -> dict[str, list[dict]]:
    """Rows as they are written on the target: new id, new names, new UUIDs."""
    rename = plan.rename
    ids: dict[str, str] = {}

    def new_uuid(v: Any) -> Any:
        if not isinstance(v, str) or not v:
            return v
        return ids.setdefault(v, str(uuid.uuid4()))

    out: dict[str, list[dict]] = {}
    for table in _TABLES:
        rows = []
        for src in db.get(table, []):
            row = dict(src)
            for key, value in src.items():
                if key == "use_case":
                    row[key] = rename.new
                elif key in {"id", "query_id", "skill_id", "session_id", "request_id"} and table != "use_cases":
                    row[key] = new_uuid(value)
                elif key in _REWRITE_TEXT_COLUMNS and isinstance(value, str):
                    row[key] = rename.text(value)
                elif isinstance(value, (dict, list)):
                    row[key] = rename.value(value)
            rows.append(row)
        out[table] = rows

    for row in out["use_cases"]:
        row.update(
            id=rename.new,
            slug=plan.slug,
            label=plan.label,
            default_collection=plan.default_collection,
            collection_prefixes=plan.prefixes,
        )
    return out


def _to_db(value: Any, data_type: str | None) -> Any:
    if value is None:
        return None
    if data_type == "jsonb":
        return json.dumps(value)
    if data_type and data_type.startswith("timestamp") and isinstance(value, str):
        return datetime.fromisoformat(value)
    if data_type == "uuid" and isinstance(value, str):
        return uuid.UUID(value)
    return value


async def write_database(db: dict[str, list[dict]], plan: ImportPlan) -> dict[str, int]:
    """All rows in one transaction — the use case appears completely or not at all."""
    rows_by_table = transform_rows(db, plan)
    new_id = plan.rename.new
    counts: dict[str, int] = {}
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if await conn.fetchval("SELECT 1 FROM use_cases WHERE id = $1", new_id):
            raise TransferError(f"Use Case „{new_id}“ existiert inzwischen bereits.")
        # A use case deleted in the dashboard leaves its config, prompts,
        # skills and memory behind (delete_use_case does not cascade). They
        # belong to nothing live, and would collide with the imported rows.
        for table in ("usecase_config", "usecase_prompts", "usecase_skills",
                      "agent_memory", "ingestion_log"):
            await conn.execute(f"DELETE FROM {table} WHERE use_case = $1", new_id)  # noqa: S608

        for table in _TABLES:
            rows = rows_by_table.get(table) or []
            if not rows:
                counts[table] = 0
                continue
            types = await _column_types(conn, table)
            columns = [c for c in rows[0] if c in types]
            placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608 — names from the schema
                f"VALUES ({placeholders})"
            )
            await conn.executemany(
                sql, [[_to_db(r.get(c), types.get(c)) for c in columns] for r in rows]
            )
            counts[table] = len(rows)

        usernames = [u["username"] for u in db.get("user_use_cases", [])]
        assigned = await conn.fetch(
            "INSERT INTO user_use_cases (user_id, use_case) "
            "SELECT id, $2 FROM users WHERE username = ANY($1::text[]) "
            "ON CONFLICT DO NOTHING RETURNING user_id",
            usernames,
            new_id,
        )
        counts["user_use_cases"] = len(assigned)
        await conn.execute("DELETE FROM deleted_use_cases WHERE use_case = $1", new_id)
    return counts


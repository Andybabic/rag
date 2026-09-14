"""Public pipeline API — bearer-token secured, asynchronous.

Reachable from outside only through the SvelteKit frontend, which forwards the
``Authorization`` header untouched; the token is verified here so there is a
single place that decides what a credential may do.

The flow is deliberately three-legged, because one agent run takes far longer
than an HTTP client should wait::

    POST /v1/public/chat            → 202 { request_id, status: "queued" }
    GET  /v1/public/chat/{id}       → { status: queued | running | done | error }
    GET  /v1/public/chat/{id}/result→ 200 the answer, or 409 while unfinished
    GET  /v1/public/sessions/{sid}  → the whole conversation as JSON

Auth model: a token carries a use-case scope (empty = all). Every endpoint
checks the scope of the *data* it touches, not just the credential, so a token
issued for one customer can never read another customer's conversation.
"""

from __future__ import annotations

import json
import logging
import uuid

import asyncpg
from core.api_tokens import ApiToken, verify_token
from core.database import get_pool
from core.jobs import create_job, get_job, get_job_result, job_to_status
from core.use_cases import get_default_collection
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/public", tags=["public-api"])

# Guardrails on one request. Images are base64 in JSON, so a handful of photos
# already makes a multi-megabyte body; without a cap a single call could pin
# the vision model for many minutes.
MAX_IMAGES = 10
MAX_QUERY_CHARS = 8000


# ── auth ──────────────────────────────────────────────────────


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_token(request: Request) -> ApiToken:
    """Resolve ``Authorization: Bearer <token>`` into a token record."""
    header = request.headers.get("authorization") or ""
    scheme, _, secret = header.partition(" ")
    if scheme.lower() != "bearer" or not secret.strip():
        raise _unauthorized("Missing bearer token")

    try:
        token = await verify_token(secret.strip())
    except asyncpg.UndefinedTableError as exc:
        # Migration 0009 has not been applied on this database yet. Say so
        # plainly instead of returning an opaque 500 the operator has to trace.
        raise HTTPException(
            status_code=503,
            detail="Public API is not initialised: run 'alembic upgrade head'",
        ) from exc

    if token is None:
        # Unknown, revoked and expired are one answer on purpose — telling them
        # apart would confirm which secrets once existed.
        raise _unauthorized("Invalid, expired or revoked token")
    return token


def _require_scope(token: ApiToken, use_case: str) -> None:
    if not token.allows(use_case):
        raise HTTPException(
            status_code=403,
            detail=f"Token is not authorised for use case '{use_case}'",
        )


# ── schemas ───────────────────────────────────────────────────
#
# Everything below that a caller can read — summaries, endpoint docstrings,
# field and response descriptions — is published verbatim as the API reference
# the dashboard renders, so it is written in German like every other
# user-facing surface of this project. Implementation comments stay English.


class ChatHistoryMessage(BaseModel):
    """Eine frühere Runde der Unterhaltung."""

    # Named distinctly from the internal ``models.HistoryMessage``: FastAPI
    # disambiguates same-named models by prefixing the module path, and
    # ``router__public__HistoryMessage`` is what an integrator would then read
    # in the generated reference.
    role: str = Field(description='`"user"` oder `"assistant"`', examples=["user"])
    content: str = Field(description="Der Nachrichtentext.")


class ChatRequest(BaseModel):
    """Eine Frage an einen Use Case, optional mit Bildern."""

    use_case: str = Field(
        description="ID des Use Case, z. B. `gw_stpoelten`. Das Token muss "
        "dafür freigegeben sein.",
        examples=["gw_stpoelten"],
    )
    query: str = Field(
        description=f"Die Frage. Höchstens {MAX_QUERY_CHARS} Zeichen.",
        examples=["Welches Werkzeug wird für Bohrungen in EN AW-6005A verwendet?"],
    )
    session_id: str | None = Field(
        default=None,
        description="UUID einer bestehenden Unterhaltung. Weglassen startet "
        "eine neue — die erzeugte ID steht in der Antwort und gehört bei "
        "Folgefragen wieder mitgeschickt.",
        examples=["2f8b1c04-7d3e-4a11-9c62-88ab0d4e1f33"],
    )
    role: str = Field(
        default="default",
        description="Rolle bzw. Perspektive, die der Use Case anbietet.",
    )
    images: list[str] = Field(
        default_factory=list,
        description=f"Bis zu {MAX_IMAGES} Bilder als Data-URI "
        "(`data:image/png;base64,…`) oder als reines base64. Sie werden vom "
        "Vision-Modell beschrieben; die Beschreibung fließt in Planung, Suche "
        "und Antwort ein.",
        examples=[["data:image/jpeg;base64,/9j/4AAQSkZJRgABA…"]],
    )
    history: list[ChatHistoryMessage] | None = Field(
        default=None,
        description="Eigener Gesprächsverlauf. **Weglassen** heißt: die "
        "vorherigen Runden der `session_id` werden automatisch verwendet. Eine "
        "Liste (auch eine leere) übernimmt die volle Kontrolle.",
    )
    config: dict = Field(
        default_factory=dict,
        description="Grenzt die Suche ein: `collection` auf genau diese eine "
        "Collection (ohne Angabe werden alle Collections des Use Case "
        "durchsucht), dazu `filters`.",
        examples=[{"collection": "gw_cnc_steps"}],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "use_case": "gw_stpoelten",
                    "query": "Welches Werkzeug für Bohren in Alu 6005?",
                },
                {
                    "use_case": "neumann",
                    "query": "Was zeigt dieses Display und was muss ich tun?",
                    "images": ["data:image/jpeg;base64,/9j/4AAQSkZJRgABA…"],
                },
            ]
        }
    }

    @field_validator("query")
    @classmethod
    def _check_query(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("query must not be empty")
        if len(text) > MAX_QUERY_CHARS:
            raise ValueError(f"query exceeds {MAX_QUERY_CHARS} characters")
        return text

    @field_validator("images")
    @classmethod
    def _check_images(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_IMAGES:
            raise ValueError(f"at most {MAX_IMAGES} images per request")
        cleaned = [img for img in value if img and img.strip()]
        for img in cleaned:
            payload = img.split(",", 1)[1] if img.startswith("data:") else img
            if not payload.strip():
                raise ValueError("image payload is empty")
        return cleaned

    @field_validator("session_id")
    @classmethod
    def _check_session(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("session_id must be a UUID") from None


# ── response models ───────────────────────────────────────────
#
# Declared as models rather than free-form dicts so the OpenAPI document the
# frontend renders describes the real response shape. The API reference is
# generated from exactly these definitions — there is no second, hand-written
# copy that could drift away from the code.


class TokenInfo(BaseModel):
    """Kenndaten des Tokens."""

    id: str = Field(examples=["1b0f7c2e-4d55-4a0b-9d31-2f6a8c1e4b77"])
    name: str = Field(
        description="Die Bezeichnung, unter der das Token angelegt wurde.",
        examples=["Intranet-Integration"],
    )
    prefix: str = Field(
        description="Erste Zeichen des Schlüssels, zum Wiedererkennen.",
        examples=["kmt_a1b2c3d4"],
    )


class TokenScope(BaseModel):
    """Was das übergebene Token darf."""

    token: TokenInfo
    use_cases: list[str] = Field(
        description="Use Cases, auf die das Token zugreifen darf. Leer = alle.",
        examples=[["gw_stpoelten"]],
    )
    scope: str = Field(
        description='`"all"` oder `"restricted"`.', examples=["restricted"]
    )
    expires_at: str | None = Field(
        description="ISO-Zeitstempel, oder null bei unbegrenzter Gültigkeit."
    )


class ChatAccepted(BaseModel):
    """Bestätigung einer eingereihten Anfrage — noch ohne Antwort."""

    request_id: str = Field(
        description="Mit dieser ID werden Zustand und Ergebnis abgefragt.",
        examples=["9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b"],
    )
    status: str = Field(
        description="`queued`, `running`, `done` oder `error`.", examples=["queued"]
    )
    use_case: str = Field(examples=["gw_stpoelten"])
    session_id: str = Field(
        description="Für eine Folgefrage wieder mitschicken.",
        examples=["2f8b1c04-7d3e-4a11-9c62-88ab0d4e1f33"],
    )
    role: str = Field(examples=["default"])
    query: str = Field(examples=["Welches Werkzeug für Bohren in Alu 6005?"])
    image_count: int = Field(examples=[0])
    created_at: str | None = Field(examples=["2026-09-08T10:12:31.114+00:00"])
    started_at: str | None = Field(
        description="Gesetzt, sobald ein Worker die Anfrage übernommen hat.",
        examples=["2026-09-08T10:12:31.980+00:00"],
    )
    finished_at: str | None = Field(examples=[None])
    error: str | None = Field(
        description="Grund, falls `status` gleich `error` ist.", examples=[None]
    )


class ChatStatus(ChatAccepted):
    """Zustand einer Anfrage. Günstig abzufragen — nie mit der Antwort."""

    result_url: str | None = Field(
        default=None,
        description="Vorhanden, sobald `status` gleich `done` ist.",
        examples=["/api/v1/chat/9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b/result"],
    )


class ChatResult(BaseModel):
    """Die fertige Antwort mit ihren Quellen."""

    request_id: str = Field(examples=["9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b"])
    status: str = Field(examples=["done"])
    use_case: str = Field(examples=["gw_stpoelten"])
    session_id: str = Field(examples=["2f8b1c04-7d3e-4a11-9c62-88ab0d4e1f33"])
    query: str = Field(examples=["Welches Werkzeug für Bohren in Alu 6005?"])
    answer: str = Field(
        description="Der Antworttext. Die Marker `[1]`, `[2]` … verweisen auf "
        "die Einträge in `citations`.",
        examples=[
            "Für Bohrungen in EN AW-6005A T6 wird der VHMI-BOHRER (T1) "
            "eingesetzt [1]."
        ],
    )
    citations: list[dict] = Field(
        description="Die Belegstellen, auf denen die Antwort beruht, samt "
        "Dokument-Metadaten.",
        examples=[
            [
                {
                    "index": 1,
                    "text": "Bearbeitungsschritt: Bohren, Durchmesser 9 mm …",
                    "metadata": {
                        "file_name": "112217126",
                        "product_id": "11221.7126",
                    },
                }
            ]
        ],
    )
    sufficient: bool = Field(
        description="Hat die Suche genug Material für eine belastbare Antwort "
        "gefunden?",
        examples=[True],
    )
    images_used: list[dict] = Field(
        description="Bilder aus der Wissensbasis, auf die sich die Antwort "
        "bezieht.",
        examples=[[]],
    )
    searched_collections: list[str] = Field(examples=[["gw_cnc_steps"]])
    created_at: str | None = Field(examples=["2026-09-08T10:12:31.114+00:00"])
    finished_at: str | None = Field(examples=["2026-09-08T10:13:04.552+00:00"])


class SessionMessage(BaseModel):
    """Eine Runde einer Unterhaltung."""

    request_id: str = Field(examples=["9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b"])
    role: str = Field(examples=["default"])
    query: str | None = Field(examples=["Welches Werkzeug für Bohren in Alu 6005?"])
    answer: str | None = Field(
        examples=["Für Bohrungen in EN AW-6005A T6 wird der VHMI-BOHRER (T1) eingesetzt [1]."]
    )
    citations: list[dict] = Field(examples=[[]])
    image_count: int = Field(examples=[0])
    sufficient: bool | None = Field(examples=[True])
    created_at: str | None = Field(examples=["2026-09-08T10:12:31.114+00:00"])
    feedback: dict | None = Field(
        description="Bewertung und Kommentar, falls abgegeben.",
        examples=[{"rating": "positive", "comment": ""}],
    )


class SessionTranscript(BaseModel):
    """Eine komplette Unterhaltung, älteste Runde zuerst."""

    session_id: str = Field(examples=["2f8b1c04-7d3e-4a11-9c62-88ab0d4e1f33"])
    use_case: str = Field(examples=["gw_stpoelten"])
    message_count: int = Field(examples=[2])
    messages: list[SessionMessage]


class ApiError(BaseModel):
    """Fehlerantwort."""

    detail: str = Field(examples=["Unbekannte request_id"])


# Reused across endpoints so every documented failure mode looks the same.
_ERR = {"model": ApiError}
_AUTH_ERRORS: dict[int | str, dict] = {
    401: {**_ERR, "description": "Token fehlt, ist unbekannt, gesperrt oder abgelaufen"},
    403: {**_ERR, "description": "Token ist für diesen Use Case nicht freigegeben"},
    503: {**_ERR, "description": "Datenbank-Migration 0009 wurde noch nicht eingespielt"},
}


# ── endpoints ─────────────────────────────────────────────────


@router.get(
    "/me",
    response_model=TokenScope,
    summary="Token prüfen",
    responses=_AUTH_ERRORS,
)
async def whoami(token: ApiToken = Depends(require_token)):
    """Meldet, was das übergebene Token darf.

    Der billigste Weg, Basis-URL und Zugangsschlüssel zu prüfen, bevor der Rest
    verdrahtet wird.
    """
    return {
        "token": {"id": token.id, "name": token.name, "prefix": token.token_prefix},
        "use_cases": token.use_cases,
        "scope": "all" if not token.use_cases else "restricted",
        "expires_at": token.expires_at,
    }


@router.post(
    "/chat",
    status_code=202,
    response_model=ChatAccepted,
    summary="Frage stellen",
    response_description="Angenommen — die Anfrage ist eingereiht",
    responses={
        400: {**_ERR, "description": "Unbekannter Use Case oder fehlerhafte Anfrage"},
        **_AUTH_ERRORS,
    },
)
async def start_chat(body: ChatRequest, token: ApiToken = Depends(require_token)):
    """Reiht eine Frage an einen Use Case ein und liefert sofort ihre Request-ID.

    Es wird nichts synchron berechnet: Ein Agentenlauf dauert Sekunden bis
    Minuten, deutlich länger als ein HTTP-Client warten sollte. Den Zustand
    liefert `GET /api/v1/chat/{request_id}`, die Antwort danach
    `GET /api/v1/chat/{request_id}/result`.

    **Bilder** werden in `images` als base64 mitgeschickt — als Data-URI oder
    roh. Sie werden zuerst vom Vision-Modell beschrieben, damit die
    Beschreibung für Planung, Suche und Antwort zur Verfügung steht.

    **Folgefragen** brauchen nur die `session_id` aus der ersten Antwort; die
    vorherigen Runden werden automatisch als Kontext ergänzt.
    """
    _require_scope(token, body.use_case)

    # Rejects unknown use cases before a job is queued, so the caller learns
    # about the typo now instead of polling a job that can only fail.
    try:
        get_default_collection(body.use_case)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session_id = body.session_id or str(uuid.uuid4())
    history = (
        [{"role": m.role, "content": m.content} for m in body.history]
        if body.history is not None
        else None
    )

    status = await create_job(
        use_case=body.use_case,
        session_id=session_id,
        query=body.query,
        role=body.role,
        images=body.images,
        history=history,
        config=body.config,
        token_id=token.id,
    )
    return status


@router.get(
    "/chat/{request_id}",
    response_model=ChatStatus,
    summary="Zustand abfragen",
    responses={404: {**_ERR, "description": "Unbekannte request_id"}, **_AUTH_ERRORS},
)
async def chat_status(
    request_id: str = Path(
        description="Die ID aus der Antwort auf die Frage.",
        examples=["9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b"],
    ),
    token: ApiToken = Depends(require_token),
):
    """Meldet, wie weit eine Anfrage ist.

    Bewusst schlank: Die Antwort selbst ist nie enthalten, deshalb darf im
    Sekundentakt abgefragt werden, ohne jedes Mal die ganze Nutzlast zu
    übertragen. Empfohlener Abstand: 2–5 Sekunden.
    """
    row = await _load_job(request_id, token)
    status = job_to_status(row)
    # A pointer beats making the caller guess the result URL from prose.
    if row["status"] == "done":
        status["result_url"] = f"/api/v1/chat/{request_id}/result"
    return status


@router.get(
    "/chat/{request_id}/result",
    response_model=ChatResult,
    summary="Antwort abholen",
    responses={
        404: {**_ERR, "description": "Unbekannte request_id"},
        409: {**_ERR, "description": "Noch eingereiht oder in Arbeit — kurz später erneut"},
        422: {**_ERR, "description": "Der Pipeline-Lauf ist gescheitert"},
        **_AUTH_ERRORS,
    },
)
async def chat_result(
    request_id: str = Path(
        description="The id returned when the question was posted.",
        examples=["9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b"],
    ),
    token: ApiToken = Depends(require_token),
):
    """Liefert die fertige Antwort samt ihren Quellen.

    Solange der Lauf nicht fertig ist, kommt **409** mit dem aktuellen Zustand
    im Body zurück. Wer lieber nur einen Endpunkt abfragt, kann also allein hier
    pollen und bei 409 einfach erneut versuchen. Ein gescheiterter Lauf
    antwortet mit **422**; der Grund steht in `detail`.
    """
    row = await _load_job(request_id, token)

    if row["status"] in ("queued", "running"):
        return JSONResponse(
            status_code=409,
            content={
                "error": "not_ready",
                "detail": "The request is still being processed",
                **job_to_status(row),
            },
        )
    if row["status"] == "error":
        return JSONResponse(
            status_code=422,
            content={
                "error": "job_failed",
                "detail": row["error"] or "unknown error",
                **job_to_status(row),
            },
        )

    result = await get_job_result(row)
    return {
        "request_id": request_id,
        "status": "done",
        "use_case": row["use_case"],
        "session_id": str(row["session_id"]),
        "query": row["query_text"],
        "answer": result.get("answer", ""),
        "citations": result.get("citations", []),
        "sufficient": result.get("sufficient", False),
        "images_used": result.get("images_used", []),
        "searched_collections": result.get("searched_collections", []),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


@router.get(
    "/chat/{request_id}/full",
    summary="Ablaufprotokoll abholen",
    response_description="Das ungekürzte Pipeline-Ergebnis",
    responses={
        404: {**_ERR, "description": "Unbekannte request_id"},
        409: {**_ERR, "description": "Der Lauf ist noch nicht fertig"},
        **_AUTH_ERRORS,
    },
)
async def chat_full_result(
    request_id: str = Path(
        description="The id returned when the question was posted.",
        examples=["9c1d3f2a-5b6e-4f70-8a91-2c3d4e5f6a7b"],
    ),
    token: ApiToken = Depends(require_token),
):
    """Liefert das ungekürzte Pipeline-Ergebnis: den Zerlegungsplan des
    Managers, jeden Sub-Agenten mit seinen Schritten, die verwendeten Chunks
    und die Laufzeiten.

    Getrennt von `/result`, weil diese Nutzlast groß ist — die meisten Aufrufer
    wollen nur die Antwort und sollen nicht bei jeder Abfrage das ganze
    Protokoll mitübertragen.
    """
    row = await _load_job(request_id, token)
    if row["status"] != "done":
        raise HTTPException(
            status_code=409, detail=f"Job is '{row['status']}', not 'done'"
        )
    return await get_job_result(row)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionTranscript,
    summary="Kompletten Chat laden",
    responses={
        400: {**_ERR, "description": "session_id ist keine UUID"},
        404: {**_ERR, "description": "Unbekannte Session"},
        **_AUTH_ERRORS,
    },
)
async def session_transcript(
    session_id: str = Path(
        description="Die Session-ID aus der Antwort auf die Frage.",
        examples=["2f8b1c04-7d3e-4a11-9c62-88ab0d4e1f33"],
    ),
    token: ApiToken = Depends(require_token),
):
    """Liefert die komplette Unterhaltung als JSON — jede Frage, Antwort,
    Quellenangabe und ein etwaiges Feedback, älteste Runde zuerst.

    Lesbar sind nur Sessions, die **über diese API** entstanden sind, und nur
    innerhalb des Use-Case-Bereichs des Tokens: Ein Token kann weder die Chats
    der Weboberfläche noch die eines anderen Kunden lesen.
    """
    try:
        session_uuid = str(uuid.UUID(session_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="session_id must be a UUID") from None

    pool = await get_pool()
    owner = await pool.fetchrow(
        """SELECT DISTINCT use_case FROM api_jobs
           WHERE session_id = $1::uuid LIMIT 1""",
        session_uuid,
    )
    if owner is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    _require_scope(token, owner["use_case"])

    rows = await pool.fetch(
        """SELECT q.id, q.use_case, q.role, q.query_text, q.answer_text,
                  q.citations, q.images, q.sufficient, q.created_at,
                  f.rating, f.comment
           FROM queries q
           LEFT JOIN feedback f ON f.query_id = q.id
           WHERE q.session_id = $1::uuid
           ORDER BY q.created_at ASC""",
        session_uuid,
    )

    def _load(value, default):
        if value is None:
            return default
        return json.loads(value) if isinstance(value, (str, bytes)) else value

    return {
        "session_id": session_uuid,
        "use_case": owner["use_case"],
        "message_count": len(rows),
        "messages": [
            {
                "request_id": str(r["id"]),
                "role": r["role"],
                "query": r["query_text"],
                "answer": r["answer_text"],
                "citations": _load(r["citations"], []),
                "image_count": len(_load(r["images"], [])),
                "sufficient": r["sufficient"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "feedback": (
                    {"rating": r["rating"], "comment": r["comment"]}
                    if r["rating"]
                    else None
                ),
            }
            for r in rows
        ],
    }


# ── generated API description ─────────────────────────────────
#
# The reference the frontend renders is derived from this service's own
# FastAPI app, so it can only describe endpoints that actually exist, with the
# parameters and response shapes the code really uses. Nothing is written twice.

_INTERNAL_PREFIX = "/v1/public"
_PUBLIC_PREFIX = "/api/v1"

_API_DESCRIPTION = """
Die Kermit-RAG-Pipeline per HTTP nutzen.

Die API ist **asynchron**: Ein Agentenlauf dauert Sekunden bis Minuten, weit
länger als ein HTTP-Client oder ein Reverse-Proxy eine Verbindung offen hält.
Eine Frage wird deshalb eingereiht und in drei Schritten beantwortet:

1. `POST /api/v1/chat` — fragen und eine `request_id` erhalten
2. `GET /api/v1/chat/{request_id}` — abfragen, bis `status` gleich `done` ist
3. `GET /api/v1/chat/{request_id}/result` — Antwort und Quellen abholen

Jeder Aufruf trägt `Authorization: Bearer <token>`. Tokens werden im Dashboard
unter **Verwaltung → API-Tokens** angelegt; sie lassen sich auf einzelne Use
Cases beschränken, befristen und sperren. Ein Token sieht ausschließlich die
Use Cases, für die es ausgestellt wurde.
"""


def _collect_refs(node: object, found: set[str]) -> None:
    """Walk a JSON-schema fragment and record every ``$ref`` it names."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


def build_public_spec(full: dict) -> dict:
    """Reduce the service's OpenAPI document to the externally reachable API.

    Two things have to change. The service's internal paths (`/v1/public/...`)
    become the addresses callers actually use (`/api/v1/...`), since the
    frontend is the only door in. And the schema list is pruned to what these
    endpoints reference — otherwise every internal model of the evaluation
    service would show up in the customer-facing reference.
    """
    paths: dict[str, dict] = {}
    for path, item in (full.get("paths") or {}).items():
        if not path.startswith(_INTERNAL_PREFIX):
            continue
        paths[_PUBLIC_PREFIX + path[len(_INTERNAL_PREFIX) :]] = item

    # Transitive closure: a referenced model may reference further models.
    referenced: set[str] = set()
    _collect_refs(paths, referenced)
    all_schemas = (full.get("components") or {}).get("schemas") or {}
    pending = list(referenced)
    while pending:
        name = pending.pop()
        nested: set[str] = set()
        _collect_refs(all_schemas.get(name, {}), nested)
        for dep in nested - referenced:
            referenced.add(dep)
            pending.append(dep)

    return {
        "openapi": full.get("openapi", "3.1.0"),
        "info": {
            "title": "Kermit Pipeline API",
            "version": full.get("info", {}).get("version", "1.0.0"),
            "description": _API_DESCRIPTION.strip(),
        },
        # Relative, so the document is correct behind any hostname or proxy.
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {
            "schemas": {k: v for k, v in all_schemas.items() if k in referenced},
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "A token created under Verwaltung → API-Tokens.",
                }
            },
        },
        # Applied globally rather than per operation: the token dependency is
        # plain code, not a FastAPI security scheme, so it leaves no per-route
        # marker — and every endpoint here requires it anyway.
        "security": [{"bearerAuth": []}],
    }


@router.get("/openapi.json", include_in_schema=False)
async def public_openapi(request: Request):
    """Machine-readable description of this API, generated from the code.

    No token required: it is a schema, not data, and the in-app documentation
    page has to be able to load it without holding an API credential.
    """
    return build_public_spec(request.app.openapi())


async def _load_job(request_id: str, token: ApiToken):
    """Load a job and check the caller may see it."""
    try:
        uuid.UUID(request_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="request_id must be a UUID") from None

    row = await get_job(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown request_id")
    _require_scope(token, row["use_case"])
    return row

"""Agent actions – each action calls external services and returns an observation."""

from __future__ import annotations

import asyncio
import logging
import os
import re

import time
import httpx
from config import settings

from core.hybrid import hybrid_fuse
from core.memory import read_memory
from core.reranker import rerank_chunks
from core.use_cases import collection_belongs_to_use_case, get_use_case_prefixes

logger = logging.getLogger(__name__)


#: Rüstunterlagen (Einstellblätter, Fotos) – mit Dateiname und Seite, also
#: das Einzige, was der Nutzer in der Quellenansicht aufschlagen kann.
_RUEST_COLLECTION = "gw_ruest_data"

# GW-Bauteilnummer („11221.7199"). Nur für Use Cases mit gw_*-Collections.
_PRODUCT_ID_RE = re.compile(r"(?<![\d.])(\d{5}\.\d{3,4})(?!\d)")


def product_scope(query: str, use_case: str) -> str | None:
    """Das eine Bauteil, um das eine Frage geht – oder ``None``.

    Nennt die Frage genau ein Bauteil, wird nur in dessen Daten gesucht: sonst
    landen Rüstangaben eines Nachbarteils in der Antwort. Mehrere genannte
    Bauteile sind ein Vergleich – dann ist das Zusammenführen gewollt.
    """
    if not collection_belongs_to_use_case("gw_cnc_steps", use_case):
        return None
    ids = set(_PRODUCT_ID_RE.findall(query or ""))
    return ids.pop() if len(ids) == 1 else None


def _source_label(meta: dict) -> str:
    """Quellenangabe eines Treffers inklusive Bauteil und Maschine.

    „Einstellblatt 1.pdf" gibt es in fast jedem Produktordner. Ohne Bauteil im
    Label kann der Agent Treffer verschiedener Produkte nicht auseinanderhalten.
    """
    file_name = meta.get("file_name", "")
    page = meta.get("page")
    source = f"{file_name}, S. {page}" if page else file_name
    extra = meta.get("extra") or {}
    pid, machine = extra.get("product_id"), extra.get("machine")
    if pid and pid not in file_name:
        source += f" · Bauteil {pid}" + (f", {machine}" if machine else "")
    return source


def _mixed_products_note(metas: list[dict]) -> str:
    """Warnung, wenn Treffer aus mehreren Bauteilen stammen."""
    products = sorted({
        pid for m in metas if (pid := (m.get("extra") or {}).get("product_id"))
    })
    if len(products) < 2:
        return ""
    return (
        f"Hinweis: Die Treffer stammen aus {len(products)} verschiedenen Bauteilen "
        f"({', '.join(products)}). Übertrage keine Angaben von einem Bauteil auf ein "
        "anderes – nenne bei jeder Angabe das Bauteil oder frage nach, welches gemeint ist."
    )


async def action_search(args: dict, *, use_case: str) -> dict:
    """SEARCH – embed query, search VectorDB, rerank results.

    Returns dict with 'observation' (str for LLM), 'chunks' (raw data for
    citations) and 'searched_collections' (list of actually searched collections).

    Hard isolation: only collections belonging to ``use_case`` are ever searched.
    """
    query = args.get("query", "")
    collection = args.get("collection", "")
    filters = args.get("filters", {})
    _embed_ms = 0
    _search_ms = 0
    _rerank_ms = 0
    _search_start = 0.0

    try:
        # 1. Get embedding
        _embed_start = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0) as client:
            embed_resp = await client.post(
                f"{settings.EMBEDDING_SERVICE_URL}/v1/embed",
                json={"type": "text", "content": query, "metadata": {},
                      "use_case": use_case},
            )
            embed_resp.raise_for_status()
            embed_json = embed_resp.json()
            vector = embed_json["vector"]
            query_embed_model = embed_json.get("model")
            _embed_ms = round((time.perf_counter() - _embed_start) * 1000)

        # 2. Resolve collections – HARD USE-CASE ISOLATION
        #    Only collections whose prefix matches the use_case are allowed.
        collections_to_search = [collection] if collection else []
        async with httpx.AsyncClient(timeout=10.0) as client:
            col_resp = await client.get(f"{settings.VECTORDB_SERVICE_URL}/v1/collections")
            col_resp.raise_for_status()
            available = [c["name"] for c in col_resp.json()["collections"]]

        if not collection:
            # No collection chosen: search every collection of this use case.
            # A use case routinely spreads over several (GW: CNC steps, setup
            # sheets and photos, material) – searching only the default one
            # left everything else unreachable.
            prefixes = get_use_case_prefixes(use_case)
            collections_to_search = [
                c for c in available if any(c.startswith(p) for p in prefixes)
            ]

        if collection and collection not in available:
            # Fallback: only collections belonging to THIS use case
            prefixes = get_use_case_prefixes(use_case)
            collections_to_search = [
                c for c in available if any(c.startswith(p) for p in prefixes)
            ]
            if not collections_to_search:
                return {
                    "observation": f"Keine Collections für Use Case '{use_case}' gefunden.",
                    "chunks": [],
                    "searched_collections": [],
                }
            logger.info(
                f"Collection '{collection}' not found, "
                f"searching use-case-scoped: {collections_to_search}"
            )
        elif collection and not collection_belongs_to_use_case(collection, use_case):
            # Requested collection exists but belongs to a different use case – block it
            logger.warning(
                f"Blocked cross-use-case access: '{collection}' not in '{use_case}'"
            )
            return {
                "observation": f"Collection '{collection}' gehört nicht zu Use Case '{use_case}'.",
                "chunks": [],
                "searched_collections": [],
            }

        if not collections_to_search:
            return {"observation": "Keine Collections verfügbar.", "chunks": [], "searched_collections": []}

        # 3. Search across resolved collections (fetch more, deduplicate later)
        # Nennt die Frage genau ein Bauteil, zuerst nur dessen Daten. Erst wenn
        # es dazu nichts gibt, wird breiter gesucht – mit einem Hinweis, damit
        # die fremden Treffer nicht als Angaben zum gefragten Bauteil gelten.
        scoped_product = None if "extra.product_id" in filters else product_scope(query, use_case)
        attempts = (
            [{**filters, "extra.product_id": scoped_product}, filters]
            if scoped_product else [filters]
        )
        scope_note = ""
        results = []
        _search_start = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt, search_filters in enumerate(attempts):
                if attempt and scoped_product:
                    scope_note = (
                        f"Hinweis: Zu Bauteil {scoped_product} gibt es keine Daten. Die "
                        f"folgenden Treffer stammen aus ANDEREN Bauteilen und gelten nicht "
                        f"für {scoped_product}."
                    )
                for col in collections_to_search:
                    search_resp = await client.post(
                        f"{settings.VECTORDB_SERVICE_URL}/v1/search",
                        json={
                            "collection": col,
                            "vector": vector,
                            "top_k": 40,
                            "filters": search_filters,
                            "embed_model": query_embed_model,
                        },
                    )
                    if search_resp.status_code == 200:
                        for r in search_resp.json()["results"]:
                            r.setdefault("metadata", {})["_collection"] = col
                            results.append(r)
                    elif search_resp.status_code == 409:
                        # Embedding-model mismatch: the index was built with a
                        # different model than this query uses → similarities are
                        # meaningless. Surface this clearly instead of letting the
                        # LLM hallucinate a "keine Information" answer.
                        body = search_resp.json()
                        logger.error(
                            "Embedding-model mismatch on '%s': index=%s query=%s. "
                            "Re-ingest required.",
                            col,
                            body.get("index_model"),
                            body.get("query_model"),
                        )
                        return {
                            "observation": (
                                f"RETRIEVAL-FEHLER: Die Collection '{col}' wurde mit "
                                f"Embedding-Modell '{body.get('index_model')}' "
                                f"indexiert, die Anfrage nutzt aber "
                                f"'{body.get('query_model')}'. Die Suche liefert "
                                f"keine verwertbaren Treffer, bis die Collection mit "
                                f"dem aktuellen Modell neu ingestiert wurde. "
                                f"Antworte NICHT aus eigenem Wissen, sondern weise "
                                f"auf dieses Konfigurationsproblem hin."
                            ),
                            "chunks": [],
                            "searched_collections": collections_to_search,
                            "embed_ms": _embed_ms,
                        }
                if results:
                    break
    except httpx.HTTPStatusError as exc:
        return {
            "observation": f"Suche fehlgeschlagen: {exc.response.status_code}. "
                           f"Beantworte die Frage mit deinem Wissen und weise darauf hin, dass keine Quellen verfügbar waren.",
            "chunks": [],
            "embed_ms": _embed_ms,
            "search_ms": _search_ms,
            "searched_collections": [],
        }
    except httpx.HTTPError as exc:
        return {
            "observation": f"Suchservice nicht erreichbar: {exc}. "
                           f"Beantworte die Frage mit deinem Wissen und weise darauf hin, dass keine Quellen verfügbar waren.",
            "chunks": [],
            "embed_ms": _embed_ms,
            "search_ms": _search_ms,
            "searched_collections": [],
        }

    _search_ms = round((time.perf_counter() - _search_start) * 1000)

    if not results:
        return {
            "observation": "Keine Ergebnisse in der Datenbank gefunden.",
            "chunks": [],
            "searched_collections": collections_to_search,
            "embed_ms": _embed_ms,
            "search_ms": _search_ms,
        }

    # 3a. Hybrid fusion: combine dense vector ranking with BM25 on the same
    #     candidate pool. Catches exact-keyword hits dense embeddings blur.
    _rerank_start = time.perf_counter()
    fused = hybrid_fuse(query, results)

    # 3b. Cross-encoder rerank on the top of the fused list (keeps things cheap)
    #     Off the event loop: the cross-encoder is several seconds of CPU work
    #     per call, and running it inline froze the whole service for that
    #     window — parallel sub-agents serialised on it and unrelated requests
    #     stalled. torch releases the GIL during inference, so a thread lets
    #     concurrent reranks genuinely overlap.
    above = await asyncio.to_thread(
        rerank_chunks, query, fused[:_RERANK_CANDIDATES], top_n=_RERANK_TOP_N
    )
    if not above:
        above = fused[:5]
    _rerank_ms = round((time.perf_counter() - _rerank_start) * 1000)

    # Deduplicate: same text from different document versions → keep highest score
    seen_texts: set[str] = set()
    unique = []
    for r in above:
        text_key = r.get("metadata", {}).get("text", "")[:150].strip()
        if text_key and text_key in seen_texts:
            continue
        seen_texts.add(text_key)
        unique.append(r)

    lines = []
    chunks = []
    for i, r in enumerate(unique[:7], 1):
        meta = r.get("metadata", {})
        text = meta.get("text", "")[:_OBSERVATION_CHARS]
        source = _source_label(meta)
        lines.append(f"[{i}] ({source}) {text}{_image_block(meta)}")
        chunks.append({
            "text": meta.get("text", ""),
            "score": r.get("rerank_score", 0.0),
            "metadata": meta,
        })
    notes = [n for n in (scope_note, _mixed_products_note([c["metadata"] for c in chunks])) if n]
    return {
        "observation": "\n".join(notes + lines),
        "chunks": chunks,
        "searched_collections": collections_to_search,
        "embed_ms": _embed_ms,
        "search_ms": _search_ms,
        "rerank_ms": _rerank_ms,
    }


# Characters of chunk text shown per observation entry.
#
# This is what the agent reasons over, so it decides not just what the agent
# can report but whether it thinks it has enough to answer at all. The chunker
# emits ~500-character chunks; at 300 the agent saw roughly the first half of
# every source and regularly judged a complete answer incomplete, spending a
# further REFINE_QUERY round on material it already had — a step costing an LLM
# call plus a full retrieval, measured at 17.6 s against roughly 500 extra
# prompt tokens for the whole observation block. Truncating here to save
# latency buys the opposite.
#
# Deliberately not unbounded: an oversized chunk (a wide table, a page with
# many appended image descriptions) must not crowd the other six out of the
# agent's context.
_OBSERVATION_CHARS = 550

# How many fused candidates the cross-encoder scores, and how many it keeps.
#
# Worth knowing before changing: only the top 7 survivors reach the agent's
# OBSERVATION, so everything scored beyond what dedup plus that cut needs is
# paid for and thrown away. On CPU each candidate costs real time — measured
# on this stack, 25 candidates take ~11 s per search, and a request doing
# three searches spent ~34 s of its ~91 s here. Lowering the candidate count
# is the cheapest speed-up available; the cost is that a chunk ranked low by
# the hybrid fusion can no longer be rescued by the reranker.
_RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "25") or 25)
_RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "15") or 15)


def _image_block(meta: dict) -> str:
    """Render the descriptions of images belonging to this chunk's page.

    In technical manuals the answer regularly exists only inside a graphic — a
    key assignment, a switch position, a table value. The chunk text around it
    says nothing about it, so an agent reading text alone cannot answer and
    reports "not in the documents" while the answer sits one layer down. The
    description is the only searchable representation of that content, so it
    travels with the chunk instead of waiting for the synthesis step.
    """
    images = ((meta.get("extra") or {}).get("images")) or []
    parts = []
    for img in images:
        alt = (img.get("alt_text") or "").strip()
        if not alt:
            continue
        img_id = img.get("id") or img.get("image_id") or ""
        parts.append(f"\n    [BILD {img_id}] {alt}")
    return "".join(parts)


def _as_text(value) -> str:
    """Ein Action-Argument als Text – egal was das Modell geliefert hat.

    Kleine Modelle schicken für ein Feld auch mal ``{"value": …}`` oder eine
    Liste. Ein ``.strip()`` darauf hat schon ganze Sub-Agents abstürzen lassen
    („'dict' object has no attribute 'strip'"), deshalb hier einmal zentral
    einsammeln statt an jeder Fundstelle.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return " ".join(p for p in (_as_text(v) for v in value) if p)
    if isinstance(value, dict):
        return " ".join(p for p in (_as_text(v) for v in value.values()) if p)
    return str(value).strip()


# Feldbezeichner, die ohne die Bauteilnummer daneben nichts mehr bedeuten
# („material": "bauteil:11221.5277" → nach dem Entfernen der Nummer Müll).
_PRODUCT_LABEL_RE = re.compile(
    r"\b(bauteil|bauteilnummer|artikel|artikelnummer|article|part|teil|nummer|nr)\b\.?:?",
    re.I,
)


# Werkzeugnummer im Programm („T5", „T12"). Bewusst eng: nur ein allein
# stehendes T mit Zahl, damit „T6-Gewinde" nicht als Nummer durchgeht.
_TOOL_ID_RE = re.compile(r"(?<![A-Za-z0-9-])T\s?(\d{1,3})(?![\d.,-])", re.I)
# Werkstoffzustand einer Aluminiumlegierung („EN AW-6005A T6", „6082 T651").
# Dasselbe Muster wie eine Werkzeugnummer und trotzdem das Gegenteil: eine
# Materialangabe. Sie wird vor der Suche nach Werkzeugnummern ausgeblendet.
_MATERIAL_TEMPER_RE = re.compile(
    r"\b(?:EN\s*)?(?:AW[-\s]?)?\d{4}[A-Z]?\s*[-–]?\s*T\d{1,3}\b", re.I
)


def _without_material_tempers(text: str) -> str:
    return _MATERIAL_TEMPER_RE.sub(" ", text or "")
_TOOL_LABEL_RE = re.compile(
    r"\b(werkzeug(?:-?nr\.?|nummer)?|tool(?:\s?no\.?|\s?id)?)\b\.?:?", re.I
)


def _strip_tool_ids(text: str) -> str:
    """Die Werkzeugnummer aus einem Freitextfeld lösen – wie die Bauteilnummer.

    „T5" als Bearbeitungsschritt zu verschlagworten bringt nichts: Im Text
    steht „Werkzeug-Nr. T5", die Bedeutung trägt der Filter, nicht die Vektor-
    Ähnlichkeit.
    """
    keep = _MATERIAL_TEMPER_RE.findall(text or "")  # Werkstoffzustand bleibt
    rest = _TOOL_LABEL_RE.sub(
        " ", _TOOL_ID_RE.sub(" ", _without_material_tempers(text))
    )
    # Der Werkstoff wird erst NACH dem Glätten wieder angehängt: „EN AW-6005A"
    # ohne Bindestrich ist nicht mehr dieselbe Legierungsbezeichnung.
    rest = re.sub(r"[\s:,;/–-]+", " ", rest).strip(" :,;/–-")
    rest = " ".join(p for p in [rest, *keep] if p).strip()
    return "" if len(rest) < 3 else rest


def _strip_product_ids(text: str) -> str:
    """Bauteilnummern (und ihre Feldbezeichner) aus einem Freitextfeld lösen.

    Als Suchtext ist „11221.5277" wertlos – semantisch steht die Nummer
    nirgends. Als Filter ist sie entscheidend. Sie gehört deshalb aus
    ``operation``/``material`` heraus und in den Bauteil-Filter hinein.
    """
    rest = _PRODUCT_LABEL_RE.sub(" ", _PRODUCT_ID_RE.sub(" ", text or ""))
    rest = re.sub(r"[\s:,;/–-]+", " ", rest).strip()
    return "" if len(rest) < 3 else rest


def _op_facet(meta: dict, key: str):
    """Read an operation facet, tolerating both nested (extra.<key>) and
    flat payload layouts."""
    extra = meta.get("extra")
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return meta.get(key)


def _fmt_tool(tool_type, diameter, tool_ids=()) -> str:
    """Name eines Werkzeugs für die Ausgabe.

    Ohne Werkzeugtyp trägt die Nummer den Namen: Im Programm steht bei
    manchen Schritten nur „Planfräsen schruppen" ohne Werkzeugangabe — welches
    Werkzeug gemeint ist, sagt dann das Einstellblatt unter derselben Nummer.
    """
    dia = ""
    if isinstance(diameter, (int, float)):
        dia = f" Ø{int(diameter) if diameter == int(diameter) else diameter} mm"
    if tool_type:
        return f"{tool_type}{dia}"
    if tool_ids:
        return (f"Werkzeug {', '.join(sorted(tool_ids))}"
                f"{dia} (Typ im Programm nicht hinterlegt)")
    return f"unbekanntes Werkzeug{dia}"


# Schneidstoff-/Werkzeug-Begriffe – KEINE Werkstücke. Das LLM verwechselt
# gern „VHM" (Vollhartmetall = Werkzeug) mit dem Werkstück-Material.
_TOOL_MATERIAL_RE = re.compile(
    r"\b(VHMI?|VHM|HM|HSS|HB|SF|FF|HARTMETALL|VOLLHARTMETALL|CBN|PKD|DIAMANT)\b",
    re.I,
)
# Durchmesser mit explizitem Marker (Ø, D, DM= oder „mm"-Suffix) – stark.
_DIA_STRONG = re.compile(
    r"(?:Ø|DM\s*=?\s*|\bD)\s*(\d{1,3}(?:[.,]\d+)?)|(\d{1,3}(?:[.,]\d+)?)\s*mm\b",
    re.I,
)
# Bloße Zahl, aber NICHT vor „Schneiden/Zähne" (das ist die Schneidenzahl) – schwach.
_DIA_WEAK = re.compile(
    r"\b(\d{1,3}(?:[.,]\d+)?)\b(?!\s*(?:schneid|zähn|zaehn))", re.I
)


def _is_tool_material(text: str) -> bool:
    """True, wenn ``text`` ein Schneidstoff/Werkzeugbegriff ist (kein Werkstück)."""
    t = text.strip()
    return bool(t) and bool(_TOOL_MATERIAL_RE.fullmatch(t))


def _parse_diameter(*texts: str) -> float | None:
    """Plausibler Werkzeugdurchmesser (mm) aus den Texten (z.B. „10 VHM-Fräser").

    Bevorzugt explizit markierte Maße (Ø/D/DM=/„mm"); fällt sonst auf eine
    bloße Zahl zurück, schließt aber Schneidenzahlen („3 Schneiden") aus.
    """
    def _first(rx: re.Pattern[str]) -> float | None:
        for text in texts:
            for m in rx.finditer(text or ""):
                raw = next((g for g in m.groups() if g), None)
                try:
                    d = float(raw.replace(",", "."))
                except (ValueError, AttributeError):
                    continue
                if 0.5 <= d <= 200:
                    return d
        return None

    return _first(_DIA_STRONG) or _first(_DIA_WEAK)


def _dia_eq(a, b, tol: float = 0.01) -> bool:
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= tol


#: So viele Chunks der Rüstunterlagen eines Bauteils gehen höchstens in die
#: Beobachtung. Deckt die Blätter eines Bauteils ab, ohne das Kontextfenster
#: mit Fotobeschreibungen zu fluten.
_RUEST_MAX_CHUNKS = 12


async def _scroll_ruest(client, product_id: str) -> list[dict]:
    """Alle Einstellblatt-Chunks eines Bauteils – ohne Ähnlichkeitsranking.

    Einstellblätter zuerst, Fotos nur, wenn dann noch Platz ist: Die
    Werkzeugliste steht auf dem Blatt, nicht auf dem Foto.
    """
    async def _page(filters: dict) -> list[dict]:
        r = await client.post(
            f"{settings.VECTORDB_SERVICE_URL}/v1/scroll",
            json={"collection": _RUEST_COLLECTION, "filters": filters,
                  "limit": 100},
        )
        if r.status_code != 200:
            return []
        return r.json().get("points", [])

    sheets = await _page({"extra.product_id": product_id,
                          "extra.doc_kind": "einstellblatt"})
    rest = []
    if len(sheets) < _RUEST_MAX_CHUNKS:
        rest = [p for p in await _page({"extra.product_id": product_id})
                if (p.get("extra") or {}).get("doc_kind") != "einstellblatt"]

    def _order(p: dict) -> tuple:
        return (str(p.get("file_name") or ""), p.get("page") or 0)

    points = sorted(sheets, key=_order) + sorted(rest, key=_order)
    # scroll liefert Payloads; auf die Trefferform von /v1/search bringen,
    # damit der Aufrufer nicht zwei Gestalten kennen muss.
    return [
        {"text": p.get("text", ""), "score": 0.0,
         "metadata": {k: v for k, v in p.items() if k != "text"}}
        for p in points[:_RUEST_MAX_CHUNKS]
    ]


async def action_search_cnc(args: dict, *, use_case: str) -> dict:
    """SEARCH_CNC – Werkzeugempfehlung für GW St. Pölten.

    Beantwortet „Mit welchem Werkzeug kann ich Bearbeitungsschritt X auf
    Material Y fertigen?" Sucht semantisch in ``gw_cnc_steps`` (die Chunks
    tragen Operation + Werkzeug + Parameter + Material), aggregiert die
    Treffer nach Werkzeug und zählt, in wie vielen historischen Bauteilen
    jedes Werkzeug für diese Operation eingesetzt wurde.

    Args:
        operation: Bearbeitungsschritt (z.B. „Taschenfräsen", „Bohren Ø10").
        product_id: optional – Bauteilnummer („11221.5277"). Gesetzt, wird nur
                    in den Programmen dieses Bauteils gesucht. Wird auch aus
                    operation/material geborgen, wo kleine Modelle sie ablegen.
        material:  Werkstoff (z.B. „EN AW-6005A T6", „Aluminium").
        missing_tool: optional – Werkzeug, das NICHT verfügbar ist; wird aus
                      den Empfehlungen ausgeschlossen (Alternativsuche).

    Hard isolation: nur für Use Cases erlaubt, die gw_*-Collections besitzen.
    """
    _embed_ms = 0
    _search_ms = 0
    if not collection_belongs_to_use_case("gw_cnc_steps", use_case):
        return {
            "observation": f"SEARCH_CNC ist für Use Case '{use_case}' nicht verfügbar.",
            "chunks": [],
            "embed_ms": _embed_ms,
            "search_ms": _search_ms,
            "searched_collections": [],
        }

    operation = _as_text(args.get("operation") or args.get("query"))
    material = _as_text(args.get("material"))
    missing_tool = _as_text(args.get("missing_tool"))

    # Bauteilnummer. Sie hat ein eigenes Argument, aber kleine Modelle stecken
    # sie genauso gern in operation ("operation": "11221.5277") oder material
    # ("material": "bauteil:11221.5277"). Von überall einsammeln, aus den
    # Textfeldern entfernen und als harten Filter nutzen – sonst beantwortet
    # die Suche die Frage zu Bauteil A mit den Werkzeugen von Bauteil B.
    scoped_product = next(
        (pid for field in (
            _as_text(args.get("product_id") or args.get("bauteil")
                     or args.get("article") or args.get("artikel")
                     or args.get("part")),
            operation,
            material,
        ) for pid in _PRODUCT_ID_RE.findall(field)),
        None,
    )
    operation = _strip_product_ids(operation)
    material = _strip_product_ids(material)

    # Werkzeugnummer („T5"). Der Rüster liest sie am Einstellblatt oder im
    # Programm ab und fragt danach — sie steht als ``extra.tool_id`` an jedem
    # Schritt. Ohne diesen Weg landet „T5" als Bearbeitungsschritt in der
    # semantischen Suche, trifft nichts, und die Antwort lautet „keine Daten",
    # obwohl die Schnittwerte im selben Chunk stehen.
    requested_tool_id = next(
        (f"T{tid}" for field in (
            _as_text(args.get("tool_id") or args.get("tool_no")
                     or args.get("werkzeug") or args.get("werkzeug_nr")),
            operation,
            missing_tool,
        ) for tid in _TOOL_ID_RE.findall(_without_material_tempers(field))),
        None,
    )
    if requested_tool_id:
        operation = _strip_tool_ids(operation)

    # Robustheit gegen Arg-Verwechslung kleiner Modelle: „VHM" o.ä. ist ein
    # Schneidstoff (Werkzeug), kein Werkstück. Niemals als Werkstück-Material
    # filtern. Liegt schon ein missing_tool vor (Alternativ-Modus), gehört der
    # Schneidstoff dorthin; sonst nur semantischer Hinweis, kein Ausschluss.
    tool_hint = ""
    if material and _is_tool_material(material):
        if missing_tool:
            missing_tool = f"{missing_tool} {material}".strip()
        else:
            tool_hint = material
        material = ""

    # Der Nutzer nennt oft einen Durchmesser („10 VHM-Fräser") – als harte
    # Eingrenzung nutzen, damit nicht Werkzeuge anderer Größe (Ø9, Ø16 …)
    # die Antwort verwässern.
    requested_dia = _parse_diameter(operation, missing_tool)

    query = " ".join(p for p in (
        f"Bearbeitungsschritt: {operation}." if operation else "",
        f"Werkzeug: {tool_hint}." if tool_hint else "",
        f"Material: {material}." if material else "",
    ) if p) or "CNC Bearbeitung Werkzeug"

    # Optional harte Material-Eingrenzung (exakte Legierung). Schlägt sie
    # fehl (kein Treffer), wird ohne Filter erneut gesucht – Robustheit vor
    # Präzision, damit nie fälschlich „nichts gefunden" herauskommt.
    material_filter = {"extra.material_class": material} if material else {}

    try:
        _embed_start = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0) as client:
            embed_resp = await client.post(
                f"{settings.EMBEDDING_SERVICE_URL}/v1/embed",
                json={"type": "text", "content": query, "metadata": {},
                      "use_case": use_case},
            )
            embed_resp.raise_for_status()
            embed_json = embed_resp.json()
            vector = embed_json["vector"]
            embed_model = embed_json.get("model")
            _embed_ms = round((time.perf_counter() - _embed_start) * 1000)

            _search_start = time.perf_counter()
            async def _search(filters: dict, collection: str = "gw_cnc_steps",
                              top_k: int = 40) -> list[dict]:
                r = await client.post(
                    f"{settings.VECTORDB_SERVICE_URL}/v1/search",
                    json={
                        "collection": collection,
                        "vector": vector,
                        "top_k": top_k,
                        "filters": filters,
                        "embed_model": embed_model,
                    },
                )
                if r.status_code != 200:
                    return []
                return r.json()["results"]

            # Eingrenzungen der Reihe nach fallen lassen, von der schärfsten
            # Kombination bis ungefiltert. Die Reihenfolge ist fachlich, nicht
            # technisch: Die Werkzeugnummer fällt VOR dem Bauteil, denn sie
            # gilt nur innerhalb eines Programms — „T5" ist in einem anderen
            # Bauteil ein völlig anderes Werkzeug. Der Werkstoff fällt zuerst,
            # er macht die Suche nur unscharf, nicht falsch.
            available = {
                "tool": {"extra.tool_id": requested_tool_id} if requested_tool_id else None,
                "product": {"extra.product_id": scoped_product} if scoped_product else None,
                "material": material_filter or None,
            }
            attempts = [
                ("tool", "product", "material"),
                ("tool", "product"),
                ("product", "material"),
                ("product",),
                ("material",),
                (),
            ]
            results: list[dict] = []
            used: tuple = ()
            seen: set[tuple] = set()
            for combo in attempts:
                keys = tuple(k for k in combo if available[k])
                if keys in seen:
                    continue
                seen.add(keys)
                search_filters: dict = {}
                for k in keys:
                    search_filters.update(available[k])
                results = await _search(search_filters)
                if results:
                    used = keys
                    break
            tool_filtered = "tool" in used
            product_filtered = "product" in used
            material_filtered = "material" in used

            # Das Einstellblatt ist das Blatt, das der Rüster an der Maschine
            # in der Hand hält – und die einzige Quelle mit Seitenzahl, an der
            # er eine Angabe nachschlagen kann. Das CNC-Programm liefert die
            # Werte, das Einstellblatt die überprüfbare Fundstelle.
            ruest_results = []
            if scoped_product and collection_belongs_to_use_case(
                _RUEST_COLLECTION, use_case
            ):
                # Vollständig lesen statt ähnlichkeitssuchen. Die Werkzeug-
                # liste eines Einstellblatts zerfällt in 500-Zeichen-Chunks,
                # und welcher davon die gesuchte Zeile trägt, entscheidet die
                # Vektorähnlichkeit nicht zuverlässig: Die Zeile „20 | Dm=63/45°
                # Messerkopf" lag mal im Treffer, mal nicht — die Antwort kippte
                # mit ihr zwischen „lagert auf Träger 50" und „gibt es nicht".
                # Ein Bauteil hat wenige Blätter; die liest man ganz.
                ruest_results = await _scroll_ruest(client, scoped_product)
            _search_ms = round((time.perf_counter() - _search_start) * 1000)
    except httpx.HTTPError as exc:
        return {
            "observation": f"Suchservice nicht erreichbar: {exc}.",
            "chunks": [],
            "embed_ms": _embed_ms,
            "search_ms": _search_ms,
            "searched_collections": [],
        }

    if not results:
        return {
            "observation": "Keine passenden CNC-Schritte in der Datenbank gefunden.",
            "chunks": [],
            "embed_ms": _embed_ms,
            "search_ms": _search_ms,
            "searched_collections": ["gw_cnc_steps"],
        }

    # Auf den angefragten Durchmesser eingrenzen (mit Fallback, falls leer).
    dia_filtered = False
    if requested_dia is not None:
        narrowed = [
            r for r in results
            if _dia_eq(_op_facet(r.get("metadata", {}), "diameter"), requested_dia)
        ]
        if narrowed:
            results, dia_filtered = narrowed, True

    # Token-basierter Ausschluss des ausgefallenen Werkzeugs (Alternativsuche).
    # Robust gegen freie Formulierungen („VHM-Fräser 3 Schneiden"). 2-Zeichen
    # zugelassen, weil Werkzeugcodes wie HB/SF/FF kurz sind – Füllwörter raus.
    _STOP = {"mm", "und", "der", "die", "das", "mit", "den", "ein", "eine"}
    missing_tokens = [
        t for t in re.findall(r"[a-zäöü]{2,}", missing_tool.lower())
        if t not in _STOP and not t.startswith("durchmess") and t not in ("schneiden", "schneide")
    ]

    # Aggregation nach Werkzeug (tool_type + Durchmesser).
    agg: dict[tuple, dict] = {}
    for r in results:
        meta = r.get("metadata", {})
        # Vorrichtungsprogramme fertigen die Spannvorrichtung, nicht das
        # Bauteil – ihre Werkzeuge gehören nicht in „in N Bauteilen verwendet".
        if _op_facet(meta, "program_role") == "vorrichtung":
            continue
        tool_type = _op_facet(meta, "tool_type")
        tool_id = _op_facet(meta, "tool_id")
        op_label = _op_facet(meta, "operation_label")
        # Ohne Typ UND ohne Nummer bleibt nichts, was man rüsten könnte –
        # solche Chunks sammelten sich früher unter „unbekanntes Werkzeug" zu
        # einem Eintrag, der über die Bauteil-Anzahl sogar das Ranking anführte.
        # Mit Nummer ist es dagegen ein echtes Werkzeug: Der Dm63 Messerkopf
        # steht im Programm von 11221.5277 als T20 ohne Typ, und ihn zu
        # verwerfen hiess „wird nicht verwendet" zu antworten, obwohl er im
        # Einstellblatt unter derselben Nummer steht.
        if not str(tool_type or "").strip() and not tool_id:
            continue
        tt_low = str(tool_type or op_label or "").lower()
        if missing_tokens and any(tok in tt_low for tok in missing_tokens):
            continue  # Alternativsuche: ausgefallenes Werkzeug ausblenden
        diameter = _op_facet(meta, "diameter")
        # Ohne Typ gruppiert die Nummer, sonst fielen alle typlosen Schritte
        # eines Bauteils in einen Topf.
        key = (str(tool_type) if tool_type else f"nr:{tool_id}", diameter)
        a = agg.setdefault(key, {
            "tool_type": tool_type, "diameter": diameter,
            "products": set(), "speeds": [], "feeds": [],
            "materials": set(), "operations": set(), "machines": set(),
            "tool_ids": set(), "labels": set(),
            "best_score": 0.0,
        })
        # Die Werkzeugnummer aus dem Programm. Sie fehlte in der Ausgabe, und
        # damit war eine Frage nach „T5" nicht zu beantworten, obwohl Nummer
        # und Schnittwerte im selben Chunk stehen.
        if tool_id:
            a["tool_ids"].add(str(tool_id).upper())
        if op_label and not tool_type:
            a["labels"].add(str(op_label))
        if pid := _op_facet(meta, "product_id"):
            a["products"].add(pid)
        if (s := _op_facet(meta, "spindle_speed")):
            a["speeds"].append(s)
        if (f := _op_facet(meta, "feed")):
            a["feeds"].append(f)
        if (mc := _op_facet(meta, "material_class")):
            a["materials"].add(mc)
        if (op := _op_facet(meta, "operation_type")):
            a["operations"].add(op)
        if (machine := _op_facet(meta, "machine")):
            a["machines"].add(machine)
        # Herkunft des besten Treffers mitführen. Ohne sie steht in der
        # Quellenansicht nur „FASENFRAESER Ø8 mm – 1 Projekte" ohne Datei und
        # ohne Zeile: nichts, was der Rüster nachschlagen könnte.
        score = r.get("score", 0.0)
        if score >= a["best_score"] or not a.get("source_meta"):
            a["source_meta"] = meta
            a["source_text"] = r.get("text", "")
        a["best_score"] = max(a["best_score"], score)

    # Ranking: häufigste Verwendung (Projekte) zuerst, dann Score.
    ranked = sorted(
        agg.values(),
        key=lambda a: (len(a["products"]), a["best_score"]),
        reverse=True,
    )

    if not ranked:
        msg = (
            "Keine alternativen Werkzeuge gefunden – die gesuchte Operation "
            "wurde historisch nur mit dem ausgeschlossenen Werkzeug gefahren."
            if missing_tokens else
            "Keine passenden Werkzeuge in der Datenbank gefunden."
        )
        return {
            "observation": msg,
            "chunks": [],
            "embed_ms": _embed_ms,
            "search_ms": _search_ms,
            "searched_collections": ["gw_cnc_steps"],
        }

    head = f"Bearbeitungsschritt: {operation or '—'}"
    if scoped_product:
        head += f" | Bauteil: {scoped_product}"
        if not product_filtered:
            head += (
                f" — ACHTUNG: zu Bauteil {scoped_product} gibt es keine CNC-Daten. "
                f"Die folgenden Werkzeuge stammen aus ANDEREN Bauteilen und gelten "
                f"nicht für {scoped_product}"
            )
    if requested_tool_id:
        head += f" | Werkzeug-Nr. {requested_tool_id}"
        if not tool_filtered:
            head += (
                f" — ACHTUNG: {requested_tool_id} ist in diesen Programmen nicht "
                f"belegt; die folgenden Werkzeuge tragen ANDERE Nummern"
            )
        elif not scoped_product:
            head += (
                " (Hinweis: Werkzeugnummern gelten je Programm — dieselbe "
                "Nummer meint in einem anderen Bauteil ein anderes Werkzeug)"
            )
    if requested_dia is not None:
        d = int(requested_dia) if requested_dia == int(requested_dia) else requested_dia
        head += f" | Ø {d} mm" + ("" if dia_filtered else (
            f" — kein Werkzeug mit Ø {d} mm vorhanden, die folgenden Optionen "
            f"haben ANDERE Durchmesser"
        ))
    if material:
        head += f" | Material: {material}"
        if not material_filtered:
            head += " (kein exakter Material-Treffer – semantische Suche)"
    if missing_tokens:
        head += f" | Alternativen ohne: {missing_tool}"
    # Bei einem einzelnen Bauteil ist die Werkzeugliste endlich (rund 20
    # Einträge) und komplett relevant. Fünf Zeilen reichten dort nicht: Der
    # gesuchte Messerkopf stand auf Platz 6 und fehlte in der Antwort, was
    # als „wird nicht verwendet" zurückkam.
    shown = 12 if scoped_product else 5
    lines = [head, f"{len(ranked)} Werkzeug-Option(en) aus historischen Programmen"
             + (f" (die ersten {shown}):" if len(ranked) > shown else ":")]

    chunks = []
    for i, a in enumerate(ranked[:shown], 1):
        n = len(a["products"])
        params = []
        if a["speeds"]:
            params.append(f"S {min(a['speeds'])}–{max(a['speeds'])}" if len(set(a["speeds"])) > 1 else f"S {a['speeds'][0]}")
        if a["feeds"]:
            params.append(f"F {min(a['feeds'])}–{max(a['feeds'])}" if len(set(a["feeds"])) > 1 else f"F {a['feeds'][0]}")
        param_str = f", Parameter {', '.join(params)}" if params else ""
        mat_str = f", Material {', '.join(sorted(a['materials']))}" if a["materials"] else ""
        ops_str = "/".join(sorted(a["operations"])) or "?"
        machine_str = f", Maschine(n) {', '.join(sorted(a['machines']))}" if a["machines"] else ""
        tid_str = (f", Werkzeug-Nr. {', '.join(sorted(a['tool_ids']))}"
                   if a["tool_ids"] else "")
        src = a.get("source_meta") or {}
        src_extra = src.get("extra") or {}
        program = src_extra.get("program_name") or src.get("file_name") or ""
        prog_str = f", Programm {program}" if program else ""
        # Fehlt der Werkzeugtyp, ist die Bezeichnung aus dem Programm der
        # einzige Hinweis darauf, was dort gefahren wurde („Planfräsen
        # schruppen") – zusammen mit der Nummer führt sie ins Einstellblatt.
        label_str = (
            f", Bezeichnung im Programm: {'; '.join(sorted(a['labels']))}"
            if a["labels"] and not a["tool_type"] else ""
        )
        lines.append(
            f"[{i}] {_fmt_tool(a['tool_type'], a['diameter'], a['tool_ids'])}"
            f"{tid_str} – in {n} Bauteil(en) für {ops_str}{param_str}{mat_str}"
            f"{machine_str}{label_str}{prog_str}"
        )
        chunks.append({
            # Der Originaltext des Schritts, nicht die Zusammenfassung: Er
            # trägt Werkzeug, Nummer, Material, Schnittwerte und die
            # Original-Bezeichnung aus dem Programm – also das, woran die
            # Angabe zu prüfen ist.
            "text": a.get("source_text")
            or f"{_fmt_tool(a['tool_type'], a['diameter'], a['tool_ids'])} "
            f"({ops_str}) – {n} Projekte",
            "score": a["best_score"],
            "metadata": {
                "file_name": src.get("file_name", ""),
                "stored_path": src.get("stored_path", ""),
                "page": src.get("page"),
                "collection": "gw_cnc_steps",
                "extra": src_extra,
                "tool_type": a["tool_type"], "diameter": a["diameter"],
                "tool_ids": sorted(a["tool_ids"]),
                "operation_type": ops_str, "project_count": n,
                "products": sorted(a["products"]),
                "machines": sorted(a["machines"]),
            },
        })

    # Ohne Treffer im gefragten Bauteil ist „Empfehlung" das falsche Wort –
    # das häufigste Werkzeug anderer Bauteile ist keine Rüstempfehlung.
    if ranked and not (scoped_product and not product_filtered):
        top = ranked[0]
        lines.append(
            f"Empfehlung: {_fmt_tool(top['tool_type'], top['diameter'], top['tool_ids'])} "
            f"(häufigstes Werkzeug für diese Operation"
            + (f", {len(top['products'])} Bauteile)." if top["products"] else ").")
        )

    # Die Einstellblätter des Bauteils hinten anhängen – als eigene,
    # aufschlagbare Quellen mit Dateiname und Seite. Das CNC-Programm sagt,
    # womit gefertigt wurde; nachprüfen kann der Rüster es nur hier.
    searched = ["gw_cnc_steps"]
    if ruest_results:
        searched.append(_RUEST_COLLECTION)
        lines.append(
            f"Rüstunterlagen zu Bauteil {scoped_product} "
            f"(dort sind die Angaben nachzuschlagen):"
        )
        # Die Einstellblätter führen eine eigene Werkzeugliste mit der Spalte
        # WZ.NR. Ob deren Nummern dieselben sind wie die T-Nummern der
        # Programme, ist im Betrieb NICHT gesichert — bei einem geprüften
        # Bauteil stimmten mehrere Zeilen überein, andere nicht. Die
        # Entscheidung gehört deshalb ins Modell, nicht in diesen Code: Es
        # sieht beide Seiten und kann Bezeichnung, Durchmesser und Operation
        # gegeneinander halten. Was es hier NICHT darf, ist stillschweigend
        # gleichsetzen.
        lines.append(
            "ZUORDNUNG: Die Einstellblätter enthalten oft eine Werkzeugliste "
            "mit eigener Spalte „WZ.NR.“. Ob diese Nummern den T-Nummern der "
            "CNC-Programme entsprechen, ist NICHT gesichert. Stelle einen "
            "Bezug nur her, wenn Bezeichnung, Durchmesser oder Operation "
            "zusammenpassen — und schreibe dann dazu, dass es eine Annahme "
            "über die Nummerngleichheit ist, die am Einstellblatt zu prüfen "
            "ist. Ein Werkzeug, dessen Typ im Programm fehlt, ist NICHT "
            "„nicht verwendet“: Die Nummer belegt den Einsatz, die "
            "Bezeichnung steht im Einstellblatt."
        )
        for r in ruest_results:
            meta = r.get("metadata", {})
            text = " ".join((r.get("text") or "").split())
            # NICHT kürzen: Die Chunks der Rüstunterlagen sind ohnehin auf
            # 500 Zeichen geschnitten, und die entscheidende Tabellenzeile
            # steht oft hinten. Gemessen: „20 | Dm=63/45° Messerkopf" lag auf
            # Position 367 und fiel einer 300-Zeichen-Kürzung zum Opfer — die
            # Antwort lautete daraufhin „wird nicht verwendet".
            lines.append(f"[{len(chunks) + 1}] ({_source_label(meta)}) {text}")
            chunks.append({
                "text": r.get("text", ""),
                "score": r.get("score", 0.0),
                "metadata": meta,
            })

    return {
        "observation": "\n".join(lines),
        "chunks": chunks,
        "searched_collections": searched,
        "embed_ms": _embed_ms,
        "search_ms": _search_ms,
    }


#: Die importierte Werkzeugliste (Bestand, Lieferant, Artikelnummer). Sie
#: kommt über den Tabellen-Import und landet dort, wo die Suche des Use Case
#: sie liest — siehe ``tableCollection`` im Frontend.
_TOOL_TABLE_COLLECTION = "gw_stpoelten_default"

#: Ab dieser Ähnlichkeit gilt ein Eintrag als dasselbe Werkzeug. Gemessen an
#: der importierten Liste (qwen3-embedding:8b): „VHM Spiralbohrer DM 4,65 mm"
#: trifft mit 0,85, „Gewindeformer M6" mit 0,73 — beide stehen drin. Der Dm63
#: Messerkopf, der NICHT in der Liste steht, kommt auf 0,58, reiner Unsinn
#: („Kühlschrank") auf 0,42. Die Vektorsuche liefert immer etwas; ohne diese
#: Grenze meldet sie einen Ø6-Fräser als Antwort auf einen Ø63 Messerkopf.
#: Bei einem anderen Embedding-Modell ist der Wert neu zu messen.
_TOOL_MATCH_MIN = 0.65

#: Spalten, die in der Beobachtung nichts zu suchen haben: interne Schlüssel
#: und Kennzahlen, die eine Rüstentscheidung nicht tragen.
_TOOL_TABLE_SKIP = {
    "kaestchenklasse", "hauptlieferant", "stellplatzmenge", "row_index",
}


def _tool_row_line(text: str) -> str:
    """Eine Tabellenzeile („Spalte: Wert" je Zeile) zu einer Zeile eindampfen."""
    parts = []
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not value or key.lower() in _TOOL_TABLE_SKIP:
            continue
        if key.lower() == "artikelnummer":
            parts.append(f"Artikel {value}")
        elif key.lower() == "bestand":
            # Der Stichtag klebt an der Zahl, nicht in einer Kopfzeile: Die
            # wurde bei der Synthese verworfen, und aus „Stand des Imports"
            # wurde in der Antwort „laut aktueller Werkzeugliste".
            parts.append(f"Bestand {value} (Stand des Listen-Imports)")
        else:
            parts.append(f"{key} {value}")
    return " · ".join(parts)


async def action_search_tool(args: dict, *, use_case: str) -> dict:
    """SEARCH_TOOL – Werkzeug im Lagerbestand nachschlagen.

    Beantwortet „Ist das Werkzeug da, und wer liefert nach?" aus der
    importierten Werkzeugliste: Artikelnummer, Bestand, Mindest- und
    Höchstbestand, Bestellmenge, Hauptlieferant.

    Bewusst getrennt von ``SEARCH_CNC``: Das CNC-Programm sagt, WOMIT gefertigt
    wurde, das Einstellblatt, WO das Werkzeug liegt, und diese Liste, OB eines
    da ist. Drei Fragen, drei Quellen.

    Der Bestand ist der Stand des zuletzt importierten Exports, keine
    Live-Abfrage. Das steht in der Beobachtung, damit die Antwort dem Rüster
    nicht zwei Stück verspricht, die im Regal längst fehlen.

    Args:
        tool: Werkzeugbezeichnung im Klartext („VHM-Bohrer 4,65", „Gewindeformer M6").
        article: optional – Artikelnummer, wenn der Nutzer sie nennt.
    """
    _embed_ms = 0
    _search_ms = 0
    if not collection_belongs_to_use_case(_TOOL_TABLE_COLLECTION, use_case):
        return {
            "observation": f"SEARCH_TOOL ist für Use Case '{use_case}' nicht verfügbar.",
            "chunks": [], "embed_ms": _embed_ms, "search_ms": _search_ms,
            "searched_collections": [],
        }

    tool = _as_text(args.get("tool") or args.get("query") or args.get("werkzeug"))
    article = _as_text(args.get("article") or args.get("artikelnummer"))
    query = " ".join(p for p in (tool, article) if p)
    if not query:
        return {
            "observation": "SEARCH_TOOL benötigt eine Werkzeugbezeichnung im Feld 'tool'.",
            "chunks": [], "embed_ms": _embed_ms, "search_ms": _search_ms,
            "searched_collections": [],
        }

    try:
        _embed_start = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0) as client:
            embed_resp = await client.post(
                f"{settings.EMBEDDING_SERVICE_URL}/v1/embed",
                json={"type": "text", "content": query, "metadata": {},
                      "use_case": use_case},
            )
            embed_resp.raise_for_status()
            embed_json = embed_resp.json()
            _embed_ms = round((time.perf_counter() - _embed_start) * 1000)

            _search_start = time.perf_counter()
            search_resp = await client.post(
                f"{settings.VECTORDB_SERVICE_URL}/v1/search",
                json={
                    "collection": _TOOL_TABLE_COLLECTION,
                    "vector": embed_json["vector"],
                    "top_k": 5,
                    "filters": {},
                    "embed_model": embed_json.get("model"),
                },
            )
            _search_ms = round((time.perf_counter() - _search_start) * 1000)
            if search_resp.status_code != 200:
                # Ohne importierte Liste ist die Collection nicht vorhanden —
                # das ist kein Fehler, sondern eine fehlende Datenquelle.
                return {
                    "observation": (
                        "Keine Werkzeugliste importiert. Bestand und Lieferant "
                        "lassen sich daher nicht nachschlagen."
                    ),
                    "chunks": [], "embed_ms": _embed_ms, "search_ms": _search_ms,
                    "searched_collections": [],
                }
            results = search_resp.json()["results"]
    except httpx.HTTPError as exc:
        return {
            "observation": f"Suchservice nicht erreichbar: {exc}.",
            "chunks": [], "embed_ms": _embed_ms, "search_ms": _search_ms,
            "searched_collections": [],
        }

    if not results:
        return {
            "observation": f'„{query}“ steht nicht in der Werkzeugliste.',
            "chunks": [], "embed_ms": _embed_ms, "search_ms": _search_ms,
            "searched_collections": [_TOOL_TABLE_COLLECTION],
        }

    best = max((r.get("score", 0.0) for r in results), default=0.0)
    if best < _TOOL_MATCH_MIN:
        lines = [
            f'„{query}“ steht NICHT in der Werkzeugliste '
            f'(beste Ähnlichkeit {best:.2f}).',
            "Die folgenden Einträge sind lediglich ähnlich und sind NICHT das "
            "gesuchte Werkzeug. Gib sie höchstens als mögliche Alternative an "
            "und sage klar, dass das gesuchte Werkzeug nicht gelistet ist.",
        ]
    else:
        lines = [
            f'Werkzeugliste (Lagerbestand) – Treffer zu „{query}“:',
            "ACHTUNG: Die Bestände sind der Stand des zuletzt importierten "
            "Exports, keine Live-Abfrage. Nenne sie immer als Stand der Liste "
            "und verweise darauf, dass am Lager zu prüfen ist.",
        ]
    chunks = []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        line = _tool_row_line(r.get("text", "")) or " ".join((r.get("text") or "").split())
        source = meta.get("file_name") or "Werkzeugliste"
        lines.append(f"[{i}] ({source}) {line}")
        chunks.append({
            "text": r.get("text", ""),
            "score": r.get("score", 0.0),
            "metadata": meta,
        })

    return {
        "observation": "\n".join(lines),
        "chunks": chunks,
        "embed_ms": _embed_ms,
        "search_ms": _search_ms,
        "searched_collections": [_TOOL_TABLE_COLLECTION],
    }


async def action_refine_query(args: dict, *, use_case: str, collection: str, filters: dict | None) -> dict:
    """REFINE_QUERY – re-run SEARCH with an LLM-rewritten query.

    Sub-agents use this to escape a mager observation without ending the
    loop: same retrieval pipeline, but with a query the agent itself has
    reformulated (synonyms, broader/narrower terms, …).

    Inherits the same use-case-scoped collection resolution as ``SEARCH``
    so isolation is never bypassed.
    """
    refined_query = (args.get("query") or "").strip()
    if not refined_query:
        return {
            "observation": "REFINE_QUERY benötigt eine neue Query im 'query'-Feld.",
            "chunks": [],
            "searched_collections": [],
        }
    reason = (args.get("reason") or "").strip()
    search_args = {
        "query": refined_query,
        "collection": collection,
        "filters": filters or {},
    }
    result = await action_search(search_args, use_case=use_case)
    if isinstance(result, dict):
        prefix = f"[Verfeinerte Suche: {reason}]\n" if reason else "[Verfeinerte Suche]\n"
        result = {**result, "observation": prefix + result.get("observation", "")}
    return result


async def action_clarify(args: dict) -> str:
    """CLARIFY – return a clarification request for the frontend."""
    question = args.get("question", "Können Sie Ihre Frage präzisieren?")
    return f"CLARIFICATION_NEEDED: {question}"


async def action_recall_memory(args: dict, use_case: str) -> str:
    """RECALL_MEMORY – read use-case memory."""
    memory = await read_memory(use_case)
    if not memory:
        return "Kein gespeicherter Kontext für diese Session."
    return f"Gespeicherter Kontext:\n{memory}"


async def action_lookup_sources(args: dict, use_case: str) -> str:
    """LOOKUP_SOURCES – list collections filtered by use case prefix."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{settings.VECTORDB_SERVICE_URL}/v1/collections")
        resp.raise_for_status()
        collections = resp.json()["collections"]

    # Filter by use case prefix patterns
    prefixes = get_use_case_prefixes(use_case)
    relevant = [c for c in collections if any(c["name"].startswith(p) for p in prefixes)]

    if not relevant:
        return "Keine Collections für diesen Use Case gefunden."

    lines = [f"Verfügbare Quellen für {use_case}:"]
    for c in relevant:
        lines.append(f"- {c['name']} ({c['count']} Chunks, {c['dimension']}D)")
    return "\n".join(lines)




# Action registry
ACTION_HANDLERS: dict[str, str] = {
    "SEARCH": "action_search",
    "SEARCH_CNC": "action_search_cnc",
    "SEARCH_TOOL": "action_search_tool",
    "REFINE_QUERY": "action_refine_query",
    "CLARIFY": "action_clarify",
    "RECALL_MEMORY": "action_recall_memory",
    "LOOKUP_SOURCES": "action_lookup_sources",
}


async def action_skill(
    args: dict,
    *,
    use_case: str,
    session_id: str,
    on_event=None,
    request_id: str | None = None,
    username: str | None = None,
) -> str:
    """Execute one skill function and return its result as an observation.

    This is the *active* path: the agent decides to call a function the way it
    decides to search, and the value comes back as an OBSERVATION it can reason
    about, combine with document chunks and cite. The text-pattern path (a call
    written into the answer) stays as a fallback for models that write one
    anyway — but it cannot do any of that, because by then the answer is done.

    Consent is enforced here, not by the caller: an action that could run a
    permission-gated function without asking would be a way around the gate.
    """
    from core.skill_functions import execute_named_function

    skill = str(args.get("skill") or "").strip()
    function = str(args.get("function") or "").strip()
    raw_args = args.get("args")
    if isinstance(raw_args, (str, int, float, bool)):
        call_args = [raw_args]
    elif isinstance(raw_args, list):
        call_args = list(raw_args)
    elif isinstance(raw_args, dict):
        # Some models emit {"ort": "Fürth"} instead of a list. Positional order
        # is the declaration order, which the function table already knows.
        call_args = list(raw_args.values())
    else:
        call_args = []

    if not skill or not function:
        return (
            "Fehler: SKILL braucht 'skill' und 'function'. "
            'Beispiel: SKILL({"skill": "wetter", "function": "aktuell", '
            '"args": ["Nürnberg"]})'
        )

    return await execute_named_function(
        skill, function, call_args,
        use_case=use_case, session_id=session_id,
        request_id=request_id, username=username, on_event=on_event,
    )

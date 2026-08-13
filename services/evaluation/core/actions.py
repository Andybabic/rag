"""Agent actions – each action calls external services and returns an observation."""

from __future__ import annotations

import logging
import re

import time
import httpx
from config import settings

from core.hybrid import hybrid_fuse
from core.memory import read_memory
from core.reranker import rerank_chunks
from core.use_cases import collection_belongs_to_use_case, get_use_case_prefixes

logger = logging.getLogger(__name__)


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
        results = []
        _search_start = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0) as client:
            for col in collections_to_search:
                search_resp = await client.post(
                    f"{settings.VECTORDB_SERVICE_URL}/v1/search",
                    json={
                        "collection": col,
                        "vector": vector,
                        "top_k": 40,
                        "filters": filters,
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
    above = rerank_chunks(query, fused[:25], top_n=15)
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
        text = meta.get("text", "")[:300]
        file_name = meta.get("file_name", "")
        page = meta.get("page")
        source = f"{file_name}, S. {page}" if page else file_name
        lines.append(f"[{i}] ({source}) {text}")
        chunks.append({
            "text": meta.get("text", ""),
            "score": r.get("rerank_score", 0.0),
            "metadata": meta,
        })
    return {
        "observation": "\n".join(lines),
        "chunks": chunks,
        "searched_collections": collections_to_search,
        "embed_ms": _embed_ms,
        "search_ms": _search_ms,
        "rerank_ms": _rerank_ms,
    }


def _op_facet(meta: dict, key: str):
    """Read an operation facet, tolerating both nested (extra.<key>) and
    flat payload layouts."""
    extra = meta.get("extra")
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return meta.get(key)


def _fmt_tool(tool_type, diameter) -> str:
    dia = ""
    if isinstance(diameter, (int, float)):
        dia = f" Ø{int(diameter) if diameter == int(diameter) else diameter} mm"
    return f"{tool_type or 'unbekanntes Werkzeug'}{dia}"


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


async def action_search_cnc(args: dict, *, use_case: str) -> dict:
    """SEARCH_CNC – Werkzeugempfehlung für GW St. Pölten.

    Beantwortet „Mit welchem Werkzeug kann ich Bearbeitungsschritt X auf
    Material Y fertigen?" Sucht semantisch in ``gw_cnc_steps`` (die Chunks
    tragen Operation + Werkzeug + Parameter + Material), aggregiert die
    Treffer nach Werkzeug und zählt, in wie vielen historischen Bauteilen
    jedes Werkzeug für diese Operation eingesetzt wurde.

    Args:
        operation: Bearbeitungsschritt (z.B. „Taschenfräsen", „Bohren Ø10").
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

    operation = (args.get("operation") or args.get("query") or "").strip()
    material = (args.get("material") or "").strip()
    missing_tool = (args.get("missing_tool") or "").strip()

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
            async def _search(filters: dict) -> list[dict]:
                r = await client.post(
                    f"{settings.VECTORDB_SERVICE_URL}/v1/search",
                    json={
                        "collection": "gw_cnc_steps",
                        "vector": vector,
                        "top_k": 40,
                        "filters": filters,
                        "embed_model": embed_model,
                    },
                )
                if r.status_code != 200:
                    return []
                return r.json()["results"]

            results = await _search(material_filter)
            material_filtered = bool(results)
            if not results and material_filter:
                results = await _search({})
                material_filtered = False
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
        tool_type = _op_facet(meta, "tool_type")
        tt_low = str(tool_type or "").lower()
        if missing_tokens and any(tok in tt_low for tok in missing_tokens):
            continue  # Alternativsuche: ausgefallenes Werkzeug ausblenden
        diameter = _op_facet(meta, "diameter")
        key = (str(tool_type), diameter)
        a = agg.setdefault(key, {
            "tool_type": tool_type, "diameter": diameter,
            "products": set(), "speeds": [], "feeds": [],
            "materials": set(), "operations": set(), "best_score": 0.0,
        })
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
        a["best_score"] = max(a["best_score"], r.get("score", 0.0))

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
    if requested_dia is not None:
        d = int(requested_dia) if requested_dia == int(requested_dia) else requested_dia
        head += f" | Ø {d} mm" + ("" if dia_filtered else " (kein exakter Ø-Treffer)")
    if material:
        head += f" | Material: {material}"
        if not material_filtered:
            head += " (kein exakter Material-Treffer – semantische Suche)"
    if missing_tokens:
        head += f" | Alternativen ohne: {missing_tool}"
    lines = [head, f"{len(ranked)} Werkzeug-Option(en) aus historischen Programmen:"]

    chunks = []
    for i, a in enumerate(ranked[:5], 1):
        n = len(a["products"])
        params = []
        if a["speeds"]:
            params.append(f"S {min(a['speeds'])}–{max(a['speeds'])}" if len(set(a["speeds"])) > 1 else f"S {a['speeds'][0]}")
        if a["feeds"]:
            params.append(f"F {min(a['feeds'])}–{max(a['feeds'])}" if len(set(a["feeds"])) > 1 else f"F {a['feeds'][0]}")
        param_str = f", Parameter {', '.join(params)}" if params else ""
        mat_str = f", Material {', '.join(sorted(a['materials']))}" if a["materials"] else ""
        ops_str = "/".join(sorted(a["operations"])) or "?"
        lines.append(
            f"[{i}] {_fmt_tool(a['tool_type'], a['diameter'])} – "
            f"in {n} Bauteil(en) für {ops_str}{param_str}{mat_str}"
        )
        chunks.append({
            "text": f"{_fmt_tool(a['tool_type'], a['diameter'])} ({ops_str}) – {n} Projekte",
            "score": a["best_score"],
            "metadata": {
                "tool_type": a["tool_type"], "diameter": a["diameter"],
                "operation_type": ops_str, "project_count": n,
                "products": sorted(a["products"]),
            },
        })

    if ranked:
        top = ranked[0]
        lines.append(
            f"Empfehlung: {_fmt_tool(top['tool_type'], top['diameter'])} "
            f"(häufigstes Werkzeug für diese Operation"
            + (f", {len(top['products'])} Bauteile)." if top["products"] else ").")
        )

    return {
        "observation": "\n".join(lines),
        "chunks": chunks,
        "searched_collections": ["gw_cnc_steps"],
        "embed_ms": _embed_ms,
        "search_ms": _search_ms,
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
    "REFINE_QUERY": "action_refine_query",
    "CLARIFY": "action_clarify",
    "RECALL_MEMORY": "action_recall_memory",
    "LOOKUP_SOURCES": "action_lookup_sources",
}

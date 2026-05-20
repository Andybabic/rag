"""Agent actions – each action calls external services and returns an observation."""

from __future__ import annotations

import logging

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

    try:
        # 1. Get embedding
        async with httpx.AsyncClient(timeout=30.0) as client:
            embed_resp = await client.post(
                f"{settings.EMBEDDING_SERVICE_URL}/v1/embed",
                json={"type": "text", "content": query, "metadata": {}},
            )
            embed_resp.raise_for_status()
            embed_json = embed_resp.json()
            vector = embed_json["vector"]
            query_embed_model = embed_json.get("model")

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
                    }
    except httpx.HTTPStatusError as exc:
        return {
            "observation": f"Suche fehlgeschlagen: {exc.response.status_code}. "
                           f"Beantworte die Frage mit deinem Wissen und weise darauf hin, dass keine Quellen verfügbar waren.",
            "chunks": [],
            "searched_collections": [],
        }
    except httpx.HTTPError as exc:
        return {
            "observation": f"Suchservice nicht erreichbar: {exc}. "
                           f"Beantworte die Frage mit deinem Wissen und weise darauf hin, dass keine Quellen verfügbar waren.",
            "chunks": [],
            "searched_collections": [],
        }

    if not results:
        return {
            "observation": "Keine Ergebnisse in der Datenbank gefunden.",
            "chunks": [],
            "searched_collections": collections_to_search,
        }

    # 3a. Hybrid fusion: combine dense vector ranking with BM25 on the same
    #     candidate pool. Catches exact-keyword hits dense embeddings blur.
    fused = hybrid_fuse(query, results)

    # 3b. Cross-encoder rerank on the top of the fused list (keeps things cheap)
    above = rerank_chunks(query, fused[:25], top_n=15)
    if not above:
        above = fused[:5]

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
    return {"observation": "\n".join(lines), "chunks": chunks, "searched_collections": collections_to_search}


async def action_search_cnc(args: dict, *, use_case: str) -> str:
    """SEARCH_CNC – cross-collection search for GW St. Pölten.

    Hard isolation: only allowed for use cases whose prefixes cover the
    gw_* collections.
    """
    # Guard: CNC-search is only valid for use cases owning gw_* collections
    if not collection_belongs_to_use_case("gw_cnc_steps", use_case):
        return f"SEARCH_CNC ist für Use Case '{use_case}' nicht verfügbar."

    ruest_id = args.get("ruest_id", "")
    filters = {}
    if ruest_id:
        filters["ruest_map_id"] = ruest_id

    # Get embedding for the query context
    query = f"CNC Rüstung {ruest_id} {args.get('missing_tool', '')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        embed_resp = await client.post(
            f"{settings.EMBEDDING_SERVICE_URL}/v1/embed",
            json={"type": "text", "content": query, "metadata": {}},
        )
        embed_resp.raise_for_status()
        vector = embed_resp.json()["vector"]

    # Cross-collection search
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.VECTORDB_SERVICE_URL}/v1/search/cross",
            json={
                "primary_collection": "gw_cnc_steps",
                "linked_collections": ["gw_ruest_data", "gw_material_info"],
                "vector": vector,
                "link_key": "cnc_step_id",
                "top_k": 5,
                "filters": filters,
            },
        )
        resp.raise_for_status()
        results = resp.json()["results"]

    if not results:
        return "Keine CNC-Schritte gefunden."

    lines = [f"Gefunden: {len(results)} ähnliche CNC-Schritte"]
    for i, r in enumerate(results[:5], 1):
        primary = r["primary"]
        meta = primary.get("metadata", {})
        op = meta.get("operation_type", "?")
        spd = meta.get("cutting_speed", "?")
        line = f"[{i}] Op: {op}, Speed: {spd}"
        linked = r.get("linked", {})
        if linked.get("gw_ruest_data"):
            line += f" | Rüst: {linked['gw_ruest_data'].get('text', '')[:80]}"
        if linked.get("gw_material_info"):
            line += f" | Material: {linked['gw_material_info'].get('text', '')[:80]}"
        lines.append(line)
    return "\n".join(lines)


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

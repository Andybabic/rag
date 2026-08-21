"""
Standalone chunk utilization analysis — does NOT touch the query pipeline.

Call after a query completes to get per-chunk utilization and answer attribution.

Claim mode: LLM extracts atomic factual claims via local Ollama (JSON schema) → embed → cosine.
Uses /v1/chat/completions with response_format:json_object for JSON output — no regex parsing needed.
Calls a local llama.cpp server on spark (not gim-ollama) to avoid reverse-proxy timeouts.
Exceptions propagate — no silent fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
import numpy as np

# In-memory progress tracker: {"query_id": {"done": N, "total": M}}
_analysis_progress: dict[str, dict[str, int]] = {}
_analysis_results: dict[str, dict] = {}

logger = logging.getLogger(__name__)

_EMBED_BATCH_SIZE = 20  # max texts per embedding request

async def _embed_batch(
    texts: list[str],
    *,
    timeout: float = 30.0,
) -> list[list[float]]:
    """Embed a list of texts via the embedding service. Splits large batches and retries on 503."""
    if not texts:
        return []

    all_vectors: list[list[float]] = []

    for batch_start in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch_texts = texts[batch_start : batch_start + _EMBED_BATCH_SIZE]
        batch = [{"chunk_id": str(i), "content": t} for i, t in enumerate(batch_texts)]

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        "http://embedding:8003/v1/embed/batch",
                        json={"chunks": batch},
                    )
                if resp.status_code == 503 and attempt < 2:
                    wait = (attempt + 1) * 2
                    logger.warning("Embedding 503, retrying in %ds (attempt %d/3)", wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                if attempt < 2:
                    wait = (attempt + 1) * 2
                    logger.warning("Embedding failed, retrying in %ds: %s", wait, exc)
                    await asyncio.sleep(wait)
                else:
                    raise

        embeddings: list = data.get("embeddings", [])
        if len(embeddings) != len(batch_texts):
            logger.warning("Embedding count mismatch: %d vs %d", len(embeddings), len(batch_texts))

        for emb in embeddings:
            vec_raw = emb.get("vector", [])
            if not vec_raw:
                all_vectors.append([])
                continue
            vec = np.array(vec_raw, dtype=np.float64)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            all_vectors.append(vec.tolist())

    return all_vectors

def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two normalised vectors (dot product)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(np.dot(np.array(a), np.array(b)))

def _build_answer_claims(
    answer_units: list[str],
    answer_matched_by: dict[int, set[int]],
    answer_best_sim: dict[int, float] | None = None,
    answer_best_nli: dict[int, str] | None = None,
    answer_best_src: dict[int, str] | None = None,
    categories: list[str] | None = None,
    confidences: list[int] | None = None,
) -> list[dict]:
    """Build answer claims list with IDs and which chunks matched each."""
    answer_claims: list[dict] = []
    for j, unit_text in enumerate(answer_units):
        matched_by = sorted(list(answer_matched_by.get(j, set())))
        claim: dict = {
            "id": f"A{j + 1}",
            "text": unit_text[:200],
            "category": categories[j] if categories and j < len(categories) else "no_category",
            "matched_by_chunks": matched_by,
        }
        if confidences and j < len(confidences):
            claim["category_confidence"] = confidences[j]
        if not matched_by and answer_best_sim and j in answer_best_sim:
            claim["best_similarity"] = round(answer_best_sim[j], 4)
            if answer_best_src and j in answer_best_src and answer_best_src[j]:
                claim["best_source_text"] = answer_best_src[j]
            if answer_best_nli and j in answer_best_nli:
                claim["best_nli"] = answer_best_nli[j]
        answer_claims.append(claim)
    return answer_claims

def _build_per_chunk_claims(
    ci: int,
    unit_matches: list[dict],
) -> list[dict]:
    """Build per-chunk claim detail list with IDs and matched_to (list of {id, sim}) or best_match for unmatched."""
    chunk_claims: list[dict] = []
    for um in unit_matches:
        if um["chunk_idx"] == ci:
            local_idx = len(chunk_claims)
            mas = um.get("matched_answer_indices", [])
            sims = um.get("matched_answer_sims", [])
            entailment_lookup = um.get("entailment_labels", {})
            if mas:
                matched_to = [{"id": f"A{ma + 1}", "sim": s, "entailment": entailment_lookup.get(str(ma), "?")} for ma, s in zip(mas, sims)]
                best_match = None
            else:
                matched_to = None
                ba = um.get("best_ans_idx", -1)
                bs = um.get("best_sim", 0.0)
                if ba >= 0 and bs > 0:
                    ent_label = entailment_lookup.get(str(ba), "?")
                    best_match = {"id": f"A{ba + 1}", "sim": bs, "entailment": ent_label}
                else:
                    best_match = None
            chunk_claims.append({
                "id": f"C{ci + 1}.{local_idx + 1}",
                "text": um["text"],
                "matched_to": matched_to,
                "best_match": best_match,
            })
    return chunk_claims

# ---------------------------------------------------------------------------
# Claim extraction via local Ollama — /api/chat with format:"json"
# ---------------------------------------------------------------------------

_LLAMACPP_CHAT_URL = "http://172.19.0.1:8081/v1/chat/completions"

# Shared persistent HTTP client with connection pooling — avoids transient
# "All connection attempts failed" errors from creating new TCP connections.
_llamacpp_client: httpx.AsyncClient | None = None

def _get_llamacpp_client(timeout: float = 90.0) -> httpx.AsyncClient:
    """Return a shared httpx.AsyncClient, creating it lazily on first use."""
    global _llamacpp_client
    if _llamacpp_client is None or _llamacpp_client.is_closed:
        _llamacpp_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
    return _llamacpp_client

_EXTRACT_SYSTEM_PROMPT = (
    "You are a fact extractor. Extract all atomic factual claims from the given text. "
    "Output ONLY valid JSON in this exact format: "
    '{"claims": ["claim 1", "claim 2", ...]}'
)

_EXTRACT_USER_TEMPLATE = "Text: {text}"

_EXTRACT_CATEGORY_PROMPT = (
    "You are a fact extractor. Extract all atomic factual claims from the given text. "
    "For each claim also pick exactly one category from: fact, verified_fact, recommendation, "
    "conclusion, no_category. fact = plain factual statement; verified_fact = factual statement "
    "that is explicitly confirmed or verified in the text; recommendation = advice, suggestion or "
    "proposed action; conclusion = a summarizing or concluding statement; no_category = anything "
    "that fits none of the above. "
    "Also provide a confidence score 0-100 for your category choice (100 = completely certain). "
    "Output ONLY valid JSON in this exact format: "
    '{"claims": [{"text": "claim text", "category": "fact", "confidence": 95}]}'
)

async def _extract_claims_via_llamacpp(
    text: str,
    *,
    model: str = "qwen3.5:9b",
    timeout: float = 90.0,
    categorize: bool = False,
) -> list[str] | list[dict]:
    """Call local Ollama via /api/chat with format:"json" for guaranteed valid JSON output."""
    if not text or not text.strip():
        return []

    text = text[:2000].strip()

    if categorize:
        claim_schema: dict = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "category": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["text", "category"],
        }
        system_prompt = _EXTRACT_CATEGORY_PROMPT
    else:
        claim_schema = {"type": "string"}
        system_prompt = _EXTRACT_SYSTEM_PROMPT

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _EXTRACT_USER_TEMPLATE.format(text=text)},
        ],
        "temperature": 0.1,
        "max_tokens": 8192,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "claims",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "claims": {
                            "type": "array",
                            "items": claim_schema,
                        }
                    },
                    "required": ["claims"],
                },
            },
        },
    }

    last_exc = None
    for attempt in range(3):
        try:
            client = _get_llamacpp_client(timeout)
            resp = await client.post(
                _LLAMACPP_CHAT_URL,
                json=payload,
                headers={"Connection": "keep-alive"},
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < 2:
                # Reset shared client on protocol errors (stale keepalive connection)
                if isinstance(exc, httpx.RemoteProtocolError):
                    global _llamacpp_client
                    if _llamacpp_client and not _llamacpp_client.is_closed:
                        await _llamacpp_client.aclose()
                    _llamacpp_client = None
                logger.warning(
                    "Llama.cpp request failed (attempt %d/3, %s), retrying in 30s...",
                    attempt + 1,
                    type(exc).__name__,
                )
                await asyncio.sleep(30.0)
            else:
                raise
        except Exception:
            raise
    else:
        raise last_exc  # type: ignore[possibly-unbound]

    msg = data.get("choices", [{}])[0].get("message", {})
    content = msg.get("content", "") or msg.get("reasoning_content", "")

    # Diagnostic: log why generation stopped so truncation cause can be identified.
    choice0 = data.get("choices", [{}])[0] if data.get("choices") else {}
    usage = data.get("usage") or {}
    logger.info(
        "LLAMACPP RESPONSE model=%s finish_reason=%s content_chars=%d prompt_tokens=%s completion_tokens=%s",
        model,
        choice0.get("finish_reason"),
        len(content or ""),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
    )

    if not content:
        return []

    # format:"json" guarantees valid JSON — but strip fences just in case
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        # Retry after stripping markdown fences (Ollama sometimes wraps anyway)
        stripped = re.sub(r"```(?:json)?\s*", "", content).strip()
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "JSON parse failed despite json_schema. Attempting regex salvage. content[:300]: %s",
                repr(content[:300]),
            )
            # Salvage claims from truncated/invalid JSON using regex
            raw = re.findall(r'"((?:[^"\\]|\\.){10,})"', content)
            if raw:
                claims = [re.sub(r"\\(.)", r"\1", c).strip() for c in raw]
                logger.warning("Salvaged %d claims via regex from malformed JSON", len(claims))
                return [c for c in claims if isinstance(c, str) and len(c.strip()) >= 10]
            return []
    claims = parsed.get("claims", [])

    if categorize:
        allowed = {"fact", "verified_fact", "recommendation", "conclusion", "no_category"}
        # Map German category names to English (LLM sometimes outputs German)
        _cat_map = {"fakt": "fact", "verifizierter_fakt": "verified_fact", "empfehlung": "recommendation",
                    "fazit": "conclusion", "schlussfolgerung": "conclusion", "keine_kategorie": "no_category"}
        out: list[dict] = []
        for c in claims:
            if isinstance(c, dict):
                text = (c.get("text") or "").strip()
                cat = (c.get("category") or "").strip().lower().replace(" ", "_")
            else:
                text = str(c).strip()
                cat = "no_category"
            cat = _cat_map.get(cat, cat)
            if cat not in allowed:
                cat = "no_category"
            raw_conf = c.get("confidence") if isinstance(c, dict) else None
            if raw_conf is not None and isinstance(raw_conf, (int, float)) and 0 <= raw_conf <= 100:
                confidence = int(raw_conf)
            else:
                confidence = None
            if len(text) >= 10:
                out.append({"text": text, "category": cat, "confidence": confidence})
        return out

    # Filter: skip empty or too-short claims
    return [c.strip() for c in claims if isinstance(c, str) and len(c.strip()) >= 10]


_MAP_CLAIMS_PROMPT = (
    "You are a text alignment expert. Given an answer text and a list of claims extracted from it, "
    "for each claim find the EXACT text span in the answer that best matches it. "
    "The claim may be paraphrased — find the closest actual wording in the answer. "
    "If a claim's content does NOT appear in the answer at all, output EMPTY for that line.\n\n"
    "Output format (one line per claim, nothing else):\n"
    "A1: <exact text from answer or EMPTY>\n"
    "A2: <exact text from answer or EMPTY>\n"
    "..."
)

_MAP_CLAIMS_USER_TEMPLATE = "ANSWER TEXT:\n<<ANSWER>>\n\nCLAIMS TO LOCATE:\n<<CLAIMS>>"


async def _map_claims_to_answer_text(
    answer_claims: list[dict],
    answer_text: str,
    *,
    model: str = "qwen3.5:9b",
    timeout: float = 90.0,
) -> dict[str, str]:
    """Ask the LLM to find exact text spans in the answer for each claim.

    Uses simple line-based format (A1: text) to avoid json_schema truncation issues.
    Returns dict mapping claim_id -> exact_text_in_answer (empty string if not found).
    """
    if not answer_claims or not answer_text:
        return {}

    claims_list = "\n".join(
        f"[{c['id']}] {c['text']}" for c in answer_claims if c.get("text")
    )
    if not claims_list:
        return {}

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _MAP_CLAIMS_PROMPT},
            {"role": "user", "content": _MAP_CLAIMS_USER_TEMPLATE.replace("<<ANSWER>>", answer_text[:6000]).replace("<<CLAIMS>>", claims_list[:4000])},
        ],
        "temperature": 0.0,
        "max_tokens": 8192,
        "stream": False,
    }

    last_exc = None
    for attempt in range(3):
        try:
            client = _get_llamacpp_client(timeout)
            resp = await client.post(
                _LLAMACPP_CHAT_URL,
                json=payload,
                headers={"Connection": "keep-alive"},
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < 2:
                if isinstance(exc, httpx.RemoteProtocolError):
                    global _llamacpp_client
                    if _llamacpp_client and not _llamacpp_client.is_closed:
                        await _llamacpp_client.aclose()
                    _llamacpp_client = None
                logger.warning(
                    "Claim mapping request failed (attempt %d/3, %s), retrying in 15s...",
                    attempt + 1,
                    type(exc).__name__,
                )
                await asyncio.sleep(15.0)
            else:
                raise
        except Exception:
            raise
    else:
        raise last_exc  # type: ignore[possibly-unbound]

    msg = data.get("choices", [{}])[0].get("message", {})
    content_raw = msg.get("content", "") or msg.get("reasoning_content", "")
    if not content_raw:
        logger.warning("_map_claims_to_answer_text: empty LLM response")
        return {}

    # Parse line-based format: "A1: exact text" or "A1: EMPTY"
    result: dict[str, str] = {}
    for line in content_raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Match "A1: text" or "A1:text"
        m = re.match(r"^(A\d+)\s*:\s*(.+)$", line)
        if m:
            cid = m.group(1)
            tex = m.group(2).strip()
            if tex.upper() == "EMPTY" or tex == "":
                result[cid] = ""
            else:
                result[cid] = tex

    logger.info("_map_claims_to_answer_text: mapped %d/%d claims (line-based)", len(result), len(answer_claims))
    return result


async def analyze_chunk_utilization_claim(
    chunks: list[dict],
    answer_text: str,
    *,
    threshold: float = 0.80,
    model: str = "qwen3.5:9b",
    max_concurrent: int = 8,
    query_id: str = "",
    mapping_model: str | None = None,
    nli_model: str = "xlm-roberta",
) -> dict:
    """
    Claim-level analysis using local Ollama for atomic fact extraction (JSON schema).

    1. Extract atomic claims from the answer via Ollama /api/chat with format:"json"
    2. Extract atomic claims from each chunk via Ollama (bounded concurrency)
    3. Embed all claims via the embedding service (batched, with retry)
    4. Match chunk-claims to answer-claims (cosine >= threshold)
    5. Compute Chunk Utilization + Answer Attribution
    6. Return per-claim match mappings with IDs for duplicate detection
    """
    if not chunks or not answer_text or not answer_text.strip():
        return {"per_chunk": [], "answer_claims": [], "summary": {"error": "no_data"}, "mode": "claim"}

    # 1. Extract claims from answer
    total_extractions = 1 + len(chunks)
    if query_id:
        current = _analysis_progress.get(query_id, {"done": 0, "total": 0})
        _analysis_progress[query_id] = {"done": current["done"], "total": current["total"] + total_extractions}

    answer_claims_data = await _extract_claims_via_llamacpp(answer_text, model=model, categorize=True)
    if query_id:
        _analysis_progress[query_id]["done"] += 1

    if not answer_claims_data:
        return {"per_chunk": [], "answer_claims": [], "summary": {"error": "no_answer_claims_extracted"}, "mode": "claim"}

    answer_claims_texts = [c["text"] for c in answer_claims_data]
    answer_claim_categories = [c.get("category", "no_category") for c in answer_claims_data]
    answer_claim_confidences = [c.get("confidence") for c in answer_claims_data]

    # 2. Extract claims from chunks with bounded concurrency
    sem = asyncio.Semaphore(max_concurrent)

    async def _extract_one(ci: int, text: str) -> tuple[int, list[str]]:
        t0 = __import__("time").perf_counter()
        logger.info("EXTRACT START chunk %d", ci)
        async with sem:
            claims = await _extract_claims_via_llamacpp(text, model=model)
            if query_id:
                _analysis_progress[query_id]["done"] += 1
            elapsed = __import__("time").perf_counter() - t0
            logger.info("EXTRACT DONE chunk %d in %.1fs", ci, elapsed)
            return ci, claims

    tasks: list[asyncio.Task] = []
    for ci, chunk in enumerate(chunks):
        text = (chunk.get("text") or "").strip()
        if text:
            tasks.append(asyncio.create_task(_extract_one(ci, text)))

    chunk_claims_map: dict[int, list[str]] = {}
    if tasks:
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for result in gathered:
            if isinstance(result, Exception):
                logger.warning("Chunk claim extraction failed: %s", result)
                continue
            ci, claims = result
            if claims:
                chunk_claims_map[ci] = claims

    if not chunk_claims_map:
        return {"per_chunk": [], "answer_claims": [], "summary": {"error": "no_chunk_claims_extracted"}, "mode": "claim"}

    # 3. Build flat claim list
    all_claims: list[str] = list(answer_claims_texts)
    claim_map: list[tuple[int, str]] = []
    for ci in sorted(chunk_claims_map.keys()):
        for claim in chunk_claims_map[ci]:
            claim_map.append((ci, claim))
            all_claims.append(claim)

    n_answer = len(answer_claims_texts)

    # 4. Embed all claims (batched with retry)
    vectors = await _embed_batch(all_claims, timeout=60.0)

    if len(vectors) != len(all_claims):
        return {"per_chunk": [], "answer_claims": [], "summary": {"error": "embedding_count_mismatch"}, "mode": "claim"}

    answer_vecs = vectors[:n_answer]
    claim_vecs = vectors[n_answer:]

    # 5. Match
    chunk_results: dict[int, dict] = {}
    answer_matched_by: dict[int, set[int]] = {}
    answer_best_sim: dict[int, float] = {}
    answer_best_src: dict[int, str] = {}  # answer claim idx -> chunk claim text that gave best_sim
    unit_matches: list[dict] = []
    chunk_best: dict[int, tuple[int, float]] = {}  # claim_map index -> (best_ans_idx, best_sim)

    for i, (ci, claim_text) in enumerate(claim_map):
        if ci not in chunk_results:
            chunk_results[ci] = {
                "rank": ci + 1,
                "chunk_text": (chunks[ci].get("text") or ""), "chunk_text_preview": (chunks[ci].get("text") or "")[:120],
                "total": 0,
                "matched": 0,
            }
        chunk_results[ci]['total'] += 1

        vec = claim_vecs[i] if i < len(claim_vecs) else []
        matches: list[dict] = []
        best_ans = -1
        best_sim_val = 0.0

        if vec:
            for j, ans_vec in enumerate(answer_vecs):
                if not ans_vec:
                    continue
                sim = _cosine(vec, ans_vec)
                if j not in answer_best_sim or sim > answer_best_sim[j]:
                    answer_best_sim[j] = sim
                    answer_best_src[j] = claim_text[:200]
                if sim > best_sim_val:
                    best_sim_val = sim
                    best_ans = j
                if sim >= threshold:
                    matches.append({"ans_idx": j, "sim": round(sim, 4)})
        matches.sort(key=lambda m: m["sim"], reverse=True)
        chunk_best[i] = (best_ans, best_sim_val)

        matched = len(matches) > 0
        if matched:
            chunk_results[ci]["matched"] += 1
            claim_id = f"C{ci + 1}.{chunk_results[ci]['total']}"
            for m in matches:
                answer_matched_by.setdefault(m["ans_idx"], set()).add(claim_id)

        um = {
            "chunk_idx": ci,
            "claim_idx": i,
            "text": claim_text[:200],
            "matched_answer_indices": [m["ans_idx"] for m in matches] if matched else [],
            "matched_answer_sims": [m["sim"] for m in matches] if matched else [],
        }
        if not matched and best_ans >= 0:
            um["best_ans_idx"] = best_ans
            um["best_sim"] = round(best_sim_val, 4)
        unit_matches.append(um)

    # 5b. NLI entailment verification via local DeBERTa-v3 model (fast + deterministic)
    COSINE_AUTOPASS = 0.90
    # NLI confidence needed to PROMOTE a sub-threshold pair (full-chunk premise is
    # loose, so require a high bar to avoid matching everything).
    NLI_OVERRIDE_CONFIDENCE = 0.90
    ent_pairs_meta: list[tuple[int, str, int, str, str]] = []  # (ci, premise_text, ai, ct40_key, answer_text)
    autopass_pairs: set[tuple[int, str, int]] = set()
    for um in unit_matches:
        ci = um["chunk_idx"]
        ct = um.get("text", "")
        # Bulk verification (cosine-matched pairs) uses the atomic claim fragment as
        # premise — keeps granularity so we can tell WHICH claim supports what.
        # The rescue path below uses the FULL chunk text: fragments lack the subject
        # (e.g. "Self-supervised losses include self-distillation." has no "SigLIP 2"),
        # which makes the model confidently NEUTRAL on genuine paraphrases.
        chunk_premise = (chunks[ci].get("text") or ct)[:1000]
        for ai, sim in zip(um.get("matched_answer_indices", []), um.get("matched_answer_sims", [])):
            if ai < len(answer_claims_texts):
                if sim >= COSINE_AUTOPASS:
                    autopass_pairs.add((ci, ct[:40], ai))
                else:
                    ent_pairs_meta.append((ci, ct, ai, ct[:40], answer_claims_texts[ai]))
        # Rescue: NLI on the BEST sub-threshold pair of unmatched chunk claims only,
        # with the full chunk as premise (a fragment premise would wrongly say NEUTRAL).
        # Deliberately NOT the xlm-roberta >=0.35 candidate sweep — that flooded the
        # result with hundreds of spurious matches when combined with the full chunk.
        if not um.get("matched_answer_indices"):
            ba = um.get("best_ans_idx", -1)
            if ba >= 0 and ba < len(answer_claims_texts):
                ent_pairs_meta.append((ci, chunk_premise, ba, ct[:40], answer_claims_texts[ba]))

    # Run local NLI model on all pairs (single call, batched internally)
    ent_pairs_text: list[tuple[str, str]] = [(ct, at) for (_, ct, _, _, at) in ent_pairs_meta]
    ent_labels: list[dict] = []
    if ent_pairs_text:
        if query_id:
            prog = _analysis_progress.get(query_id, {"done": 0, "total": 0})
            _analysis_progress[query_id] = {"done": prog["done"], "total": prog["total"] + 1}
        ent_labels = await asyncio.to_thread(_classify_entailment_pairs, ent_pairs_text, nli_model=nli_model)
        logger.info("NLI classified %d pairs with model=%s — entailment: %d, neutral: %d, contradiction: %d", len(ent_labels), nli_model, sum(1 for l in ent_labels if l.get("label")=="ENTAILMENT"), sum(1 for l in ent_labels if l.get("label")=="NEUTRAL"), sum(1 for l in ent_labels if l.get("label")=="CONTRADICTION"))
        if query_id:
            _analysis_progress[query_id]["done"] += 1

    # Build lookup: (ci, ct[:40], ai) -> label
    entailment_labels_all: dict[tuple[int, str, int], str] = {}
    for k, (ci, _, ai, ct40, _) in enumerate(ent_pairs_meta):
        entailment_labels_all[(ci, ct40, ai)] = ent_labels[k] if k < len(ent_labels) else "NEUTRAL"

    # Filter: only keep ENTAILMENT-verified or autopass matches
    for um in unit_matches:
        ci = um["chunk_idx"]
        ct = um.get("text", "")
        old_indices = um.get("matched_answer_indices", [])
        old_sims = um.get("matched_answer_sims", [])
        new_indices: list[int] = []
        new_sims: list[float] = []
        labels_for_display: dict[str, str] = {}
        for ai, sim in zip(old_indices, old_sims):
            key = (ci, ct[:40], ai)
            if key in autopass_pairs:
                labels_for_display[str(ai)] = "ENTAILMENT (autopass)"
                new_indices.append(ai)
                new_sims.append(sim)
            else:
                label_dict = entailment_labels_all.get(key, {"label": "NEUTRAL", "conf": 0.0})
                labels_for_display[str(ai)] = f"{label_dict['label']} ({round(label_dict['conf']*100)}%)"
                if label_dict["label"] == "ENTAILMENT":
                    new_indices.append(ai)
                    new_sims.append(sim)
        um["matched_answer_indices"] = new_indices
        um["matched_answer_sims"] = new_sims
        um["entailment_labels"] = labels_for_display
        # Scan all sub-threshold NLI pairs for promotion + display
        if not um.get("matched_answer_indices"):
            promoted_indices: list[int] = []
            promoted_sims: list[float] = []
            first_best = um.get("best_ans_idx", -1)
            for (ci_key, ct_key, ai_key), label_dict in entailment_labels_all.items():
                if ci_key != ci or ct_key != ct[:40]:
                    continue
                if ai_key == first_best:
                    labels_for_display[str(ai_key)] = f"{label_dict['label']} ({round(label_dict['conf']*100)}%)"
                if label_dict["label"] == "ENTAILMENT" and label_dict["conf"] >= NLI_OVERRIDE_CONFIDENCE:
                    # Find cosine for this pair
                    sim_for_ai = um.get("best_sim", 0) if ai_key == first_best else 0.0
                    if sim_for_ai == 0.0 and ai_key < len(answer_vecs):
                        claim_i = um.get("claim_idx", -1)
                        if claim_i >= 0 and claim_i < len(claim_vecs) and claim_vecs[claim_i]:
                            sim_for_ai = round(_cosine(claim_vecs[claim_i], answer_vecs[ai_key]), 4)
                    if sim_for_ai > 0:
                        promoted_indices.append(ai_key)
                        promoted_sims.append(sim_for_ai)
                        logger.info("NLI OVERRIDE: promoted sub-threshold pair C%d->A%d (cos=%.3f, nli=%s %.0f%%)", ci+1, ai_key+1, sim_for_ai, label_dict["label"], label_dict["conf"]*100)
            if promoted_indices:
                # Sort by sim descending
                paired = sorted(zip(promoted_indices, promoted_sims), key=lambda x: x[1], reverse=True)
                um["matched_answer_indices"] = [p[0] for p in paired]
                um["matched_answer_sims"] = [p[1] for p in paired]

        # Rebuild chunk_results matched counts from filtered matches
        for ci in sorted(chunk_results.keys()):
            cr = chunk_results[ci]
            cr["matched"] = sum(
                1 for um in unit_matches
                if um["chunk_idx"] == ci and len(um.get("matched_answer_indices", [])) > 0
            )

        # Rebuild answer_matched_by with proper claim IDs
        answer_matched_by = {}
        claim_counter: dict[int, int] = {}
        for um in unit_matches:
            ci = um["chunk_idx"]
            ct = um.get("text", "")
            ccount = claim_counter.get(ci, 0) + 1
            claim_counter[ci] = ccount
            claim_id = f"C{ci + 1}.{ccount}"
            for ai in um.get("matched_answer_indices", []):
                key = (ci, ct[:40], ai)
                if key in autopass_pairs or entailment_labels_all.get(key, {"label": "NEUTRAL", "conf": 0.0})["label"] == "ENTAILMENT":
                    answer_matched_by.setdefault(ai, set()).add(claim_id)

    # Compute best NLI label per answer claim (for unmatched claims display)
    answer_best_nli: dict[int, str] = {}
    for um in unit_matches:
        ci = um["chunk_idx"]
        ct = um.get("text", "")
        for ai, sim in zip(um.get("matched_answer_indices", []), um.get("matched_answer_sims", [])):
            if ai in answer_best_sim and abs(sim - answer_best_sim[ai]) < 0.0001:
                key = (ci, ct[:40], ai)
                if key in autopass_pairs:
                    answer_best_nli[ai] = "ENTAILMENT (autopass)"
                else:
                    label_dict = entailment_labels_all.get(key, {"label": "NEUTRAL", "conf": 0.0})
                    answer_best_nli[ai] = f"{label_dict['label']} ({round(label_dict['conf']*100)}%)"

    # 6. Build output
    answer_claims = _build_answer_claims(answer_claims_texts, answer_matched_by, answer_best_sim, answer_best_nli=answer_best_nli, answer_best_src=answer_best_src, categories=answer_claim_categories, confidences=answer_claim_confidences)

    # 6b. Map claims to exact answer text spans via LLM (for reliable highlighting)
    try:
        claim_text_map = await _map_claims_to_answer_text(answer_claims, answer_text, model=mapping_model or model)
        for ac in answer_claims:
            mapped = claim_text_map.get(ac["id"], "")
            ac["text_in_answer"] = mapped if mapped and mapped.strip() else ""
    except Exception:
        logger.warning("Claim-to-answer mapping failed, falling back to claim text for highlighting", exc_info=True)
        for ac in answer_claims:
            ac["text_in_answer"] = ""

    per_chunk: list[dict] = []
    for ci in sorted(chunk_results.keys()):
        cr = chunk_results[ci]
        total = cr["total"]
        matched = cr["matched"]
        utilization = round(matched / total * 100, 1) if total > 0 else 0.0
        attributed = sum(1 for ans_idx, gis in answer_matched_by.items() if any(cid.startswith(f"C{ci + 1}.") for cid in gis))
        attribution = round(attributed / n_answer * 100, 1) if n_answer > 0 else 0.0
        per_chunk.append({
            "rank": cr["rank"],
            "chunk_text": cr["chunk_text"], "chunk_text_preview": cr["chunk_text_preview"],
            "total_units": total,
            "matched_units": matched,
            "utilization_pct": utilization,
            "attribution_pct": attribution,
            "claims": _build_per_chunk_claims(ci, unit_matches),
        })

    utils = [c["utilization_pct"] for c in per_chunk]
    attrs = [c["attribution_pct"] for c in per_chunk]

    return {
        "mode": "claim",
        "entailment_verified": len(ent_pairs_text) > 0,
        "per_chunk": per_chunk,
        "answer_claims": answer_claims,
        "summary": {
            "avg_utilization_pct": round(sum(utils) / len(utils), 1) if utils else 0,
            "avg_attribution_pct": round(sum(attrs) / len(attrs), 1) if attrs else 0,
            "total_chunk_units": sum(c["total_units"] for c in per_chunk),
            "total_answer_units": n_answer,
            "total_matched": sum(c["matched_units"] for c in per_chunk),
            "threshold": threshold,
        },
    }



# ---------------------------------------------------------------------------
# Entailment Verification — local DeBERTa-v3 NLI model (fast, deterministic)
# ---------------------------------------------------------------------------

_nli_model = None
_nli_model_name: str = "deberta"


def _get_nli_model(model_name: str = "deberta"):
    """Lazy-load the NLI cross-encoder model (deberta=English-only/fast, mdeberta=multilingual)."""
    global _nli_model, _nli_model_name
    if _nli_model is None or _nli_model_name != model_name:
        from sentence_transformers import CrossEncoder
        if model_name == "xlm-roberta":
            _nli_model = CrossEncoder('MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7')
        else:
            _nli_model = CrossEncoder('cross-encoder/nli-deberta-v3-base')
        _nli_model_name = model_name
    return _nli_model


def _classify_entailment_pairs(pairs: list[tuple[str, str]], nli_model: str = "deberta") -> list[str]:
    """Classify (premise, hypothesis) pairs via DeBERTa-v3 NLI model.
    
    Returns list of {"label": str, "conf": float} dicts.
    Fast (~10ms/pair), deterministic, no LLM involved.
    """
    if not pairs:
        return []
    model = _get_nli_model(nli_model)
    # CrossEncoder.predict returns logits (n_pairs, n_classes)
    scores = model.predict(pairs, show_progress_bar=False, batch_size=32)
    id2label = model.model.config.id2label
    labels = []
    for score_row in scores:
        idx = int(np.argmax(score_row))
        # Softmax for confidence
        exp_scores = np.exp(score_row - np.max(score_row))
        conf = float(exp_scores[idx] / exp_scores.sum())
        raw_label = id2label[idx].upper()
        if 'ENTAIL' in raw_label:
            labels.append({"label": "ENTAILMENT", "conf": conf})
        elif 'CONTRADICT' in raw_label:
            labels.append({"label": "CONTRADICTION", "conf": conf})
        else:
            labels.append({"label": "NEUTRAL", "conf": conf})
    return labels

"""Reranking – cross-encoder (bge-reranker-v2-m3, Qwen3-Reranker, …) with a safe fallback.

The cross-encoder scores each (query, chunk) pair jointly, which is far more
discriminating than cosine similarity between separate embeddings. This is the
biggest quality lever once the vector space is dense. Model is lazy-loaded and
cached; if loading fails (no model on disk, no torch, etc.), we fall back to
the existing fused-score ordering so the service never hard-fails on retrieval.

Backends, picked via ``RERANKER_TYPE`` (default ``auto`` = local):

- ``cross_encoder``: sentence-transformers ``CrossEncoder``, loaded in the
  process. Covers bge-reranker as well as Qwen3-Reranker (sentence-transformers
  >= 6 applies Qwen's prompt template itself).
- ``api``: a remote rerank endpoint (vLLM, Infinity, LiteLLM, Cohere/Jina-style
  ``POST {RERANKER_API_URL}/rerank``), e.g. on a GPU host. Nothing is loaded
  locally; every request goes over HTTP.

Scores are logits everywhere (raw for local models, log-odds from the API's
probabilities), the scale ``_relevance_label`` is tuned for.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_RERANKER_MODEL_NAME = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
_RERANKER_TYPE = (os.getenv("RERANKER_TYPE", "auto") or "auto").strip().lower()
# Task description sent to a remote Qwen3-Reranker with every pair. Qwen
# recommends an English instruction even for non-English queries.
_RERANKER_INSTRUCTION = os.getenv(
    "RERANKER_INSTRUCTION",
    "Given a question, judge whether the passage contains information that answers it",
)
# Truncation length per (query, chunk) pair. The cross-encoder is the single
# most expensive step of a request on CPU, and its cost scales with this.
# Chunks are ~256 tokens by default, so 512 mostly pays for padding — but
# lowering it below the chunk size silently truncates evidence, so it stays a
# deliberate setting rather than a quiet default change.
_RERANKER_MAX_LENGTH = int(os.getenv("RERANKER_MAX_LENGTH", "512") or 512)
# Remote backend (RERANKER_TYPE=api). The URL is the API base, i.e. what comes
# before "/rerank" – for vLLM typically "http://<host>:8000/v1".
_RERANKER_API_URL = (os.getenv("RERANKER_API_URL", "") or "").rstrip("/")
_RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "") or ""
_RERANKER_API_TIMEOUT = float(os.getenv("RERANKER_API_TIMEOUT", "30") or 30)
# How query/document are sent: "raw", or "qwen3" = wrapped in the Qwen3-Reranker
# prompt here (vLLM's own example does exactly this). "auto" picks qwen3 by
# model name. Use "raw" if the server applies the template itself.
_RERANKER_API_PROMPT = (os.getenv("RERANKER_API_PROMPT", "auto") or "auto").strip().lower()
_RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() in ("1", "true", "yes")

# Qwen3-Reranker prompt scaffolding (model card), used by the API backend.
_QWEN3_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements "
    "based on the Query and the Instruct provided. Note that the answer can "
    'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
_QWEN3_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

_model = None
_model_lock = threading.Lock()
_model_load_failed = False
_model_loading = False  # True while the background loader thread is running


def _relevance_label(score: float) -> str:
    """Map a score to a human-readable relevance label (cross-encoder scores
    are typically logits roughly in [-10, 10]; thresholds tuned for bge-reranker)."""
    if score >= 3.0:
        return "highly_relevant"
    if score >= 0.0:
        return "relevant"
    if score >= -3.0:
        return "marginally_relevant"
    return "not_relevant"


def _resolved_type() -> str:
    """Backend: ``api`` only when asked for, otherwise the local cross-encoder."""
    return "api" if _RERANKER_TYPE == "api" else "cross_encoder"


def _load_cross_encoder():
    import torch
    from sentence_transformers import CrossEncoder

    # activation_fn=Identity: raw logits. sentence-transformers >= 4 applies a
    # sigmoid by default, which squeezed every score into [0, 1] – so
    # _relevance_label called everything "relevant" (>= 0.0) and nothing
    # "highly_relevant" (>= 3.0). Ordering is unaffected either way.
    #
    # float32: Qwen3-Reranker ships as bfloat16, and bf16 matmuls on CPU
    # measured ~2.5x slower (24 s vs 9.6 s for 15 pairs) at the same ranking.
    return CrossEncoder(
        _RERANKER_MODEL_NAME,
        max_length=_RERANKER_MAX_LENGTH,
        activation_fn=torch.nn.Identity(),
        model_kwargs={"dtype": torch.float32},
    )


def _logit(p: float) -> float:
    """Probability → log-odds, the scale ``_relevance_label`` is tuned for."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


class _ApiReranker:
    """Remote reranker behind the ``CrossEncoder.predict`` interface.

    Speaks the Cohere/Jina rerank contract that vLLM, Infinity and LiteLLM
    all serve: ``{"model", "query", "documents"}`` in, ``{"results": [{"index",
    "relevance_score"}]}`` out. ``relevance_score`` is a probability in [0, 1]
    there, so it is mapped to log-odds to keep the shared label thresholds.
    """

    def __init__(self, base_url: str, model_name: str) -> None:
        if not base_url:
            raise ValueError("RERANKER_TYPE=api needs RERANKER_API_URL")
        self._url = f"{base_url}/rerank"
        self._model_name = model_name
        self._headers = (
            {"Authorization": f"Bearer {_RERANKER_API_KEY}"} if _RERANKER_API_KEY else {}
        )
        prompt = _RERANKER_API_PROMPT
        if prompt == "auto":
            prompt = "qwen3" if "qwen3-reranker" in model_name.lower() else "raw"
        self._qwen3_prompt = prompt == "qwen3"

    def served_models(self) -> list[str]:
        """Model ids the server offers (OpenAI-style ``GET /models``)."""
        base = self._url.removesuffix("/rerank")
        try:
            resp = httpx.get(f"{base}/models", headers=self._headers, timeout=10)
            return [m["id"] for m in resp.json().get("data", [])]
        except Exception:  # noqa: BLE001
            return []

    def predict(self, pairs: list[tuple[str, str]], show_progress_bar: bool = False) -> list[float]:
        if not pairs:
            return []
        query = pairs[0][0]
        docs = [d for _, d in pairs]
        if self._qwen3_prompt:
            query = (
                f"{_QWEN3_PREFIX}<Instruct>: {_RERANKER_INSTRUCTION}\n"
                f"<Query>: {query}\n"
            )
            docs = [f"<Document>: {d}{_QWEN3_SUFFIX}" for d in docs]
        resp = httpx.post(
            self._url,
            json={"model": self._model_name, "query": query, "documents": docs},
            headers=self._headers,
            timeout=_RERANKER_API_TIMEOUT,
        )
        if resp.status_code >= 400:
            # The server's own message ("The model `x` does not exist.") says
            # what is wrong; a bare "404 Not Found" does not.
            try:
                detail = resp.json().get("error", {}).get("message") or resp.text
            except ValueError:
                detail = resp.text
            raise RuntimeError(f"HTTP {resp.status_code} von {self._url}: {detail[:300]}")
        scores = [0.0] * len(pairs)
        seen = 0
        for item in resp.json()["results"]:
            scores[item["index"]] = _logit(float(item["relevance_score"]))
            seen += 1
        if seen != len(pairs):
            # A partial answer would rank the missing chunks as "unsure"
            # instead of reporting the failure – treat it as one.
            raise ValueError(f"rerank API scored {seen} of {len(pairs)} documents")
        return scores


def _load_model_blocking() -> None:
    """Actually load the model. Runs in a background thread."""
    global _model, _model_load_failed, _model_loading  # noqa: PLW0603
    try:
        backend = _resolved_type()
        if backend == "api":
            model = _ApiReranker(_RERANKER_API_URL, _RERANKER_MODEL_NAME)
        else:
            model = _load_cross_encoder()
        with _model_lock:
            _model = model
        logger.info("Loaded reranker model: %s (%s)", _RERANKER_MODEL_NAME, backend)
    except Exception as exc:  # noqa: BLE001
        with _model_lock:
            _model_load_failed = True
        # ERROR, not WARNING: without the cross-encoder the system runs
        # permanently in a much weaker retrieval mode. This must be visible.
        logger.error(
            "Reranker FAILED to load (%s): %s. Retrieval quality is degraded "
            "(no cross-encoder reranking). Ensure 'sentence-transformers' + "
            "torch are installed and the model '%s' is reachable/cached "
            "(set RERANKER_MODEL or pre-download into the image/volume).",
            type(exc).__name__,
            exc,
            _RERANKER_MODEL_NAME,
        )
    finally:
        with _model_lock:
            _model_loading = False


def preload_model() -> None:
    """Kick off the model download/load in a background thread.

    Call this once at service startup. Until the model is ready, requests fall
    back to the hybrid-fusion ordering instead of blocking the event loop.
    """
    global _model_loading  # noqa: PLW0603
    if not _RERANKER_ENABLED:
        logger.info("Reranker disabled via RERANKER_ENABLED=false")
        return
    with _model_lock:
        if _model is not None or _model_load_failed or _model_loading:
            return
        _model_loading = True
    logger.info("Preloading reranker model in background: %s", _RERANKER_MODEL_NAME)
    threading.Thread(
        target=_load_model_blocking, name="reranker-loader", daemon=True
    ).start()


def reranker_status() -> dict:
    """Current reranker state – surfaced via the health endpoint so the
    degraded mode is verifiable without log access."""
    with _model_lock:
        if not _RERANKER_ENABLED:
            state = "disabled"
        elif _model is not None:
            state = "ready"
        elif _model_load_failed:
            state = "failed"
        elif _model_loading:
            state = "loading"
        else:
            state = "not_started"
    return {
        "enabled": _RERANKER_ENABLED,
        "model": _RERANKER_MODEL_NAME,
        "type": _resolved_type(),
        "api_url": _RERANKER_API_URL if _resolved_type() == "api" else None,
        "max_length": _RERANKER_MAX_LENGTH,
        "state": state,
        # When not "ready", retrieval falls back to hybrid-fusion order only.
        "degraded": state not in ("ready", "disabled"),
    }


def _get_model():
    """Return the loaded cross-encoder, or None if not ready / unavailable.

    Non-blocking: if the model is still downloading, this returns None and the
    caller uses the fallback path. As soon as the background loader finishes,
    subsequent calls will start using the real model.
    """
    if not _RERANKER_ENABLED:
        return None
    with _model_lock:
        if _model is not None:
            return _model
        if _model_load_failed or _model_loading:
            return None
        # Not preloaded (e.g. import from a script) – kick off loader once.
    preload_model()
    return None


_PROBE_QUERY = "Wie hoch ist der maximale Betriebsdruck der Hydraulik?"
_PROBE_RELEVANT = "The hydraulic system operates at a maximum pressure of 10 bar."
_PROBE_IRRELEVANT = "Taylor Swift played eight sold-out shows at Wembley Stadium in London."


def probe_reranker() -> dict:
    """Score one relevant and one irrelevant passage (German query, English
    text – the cross-lingual case retrieval depends on).

    Crashing is not the only failure: a wrong score scale (probabilities
    instead of logits) or a missing/doubled Qwen3 prompt template return
    numbers that look fine and mean little. Merely ranking the relevant
    passage first is not enough – measured against vLLM without the template,
    both passages came out "relevant" (2.45 vs. 0.15). So the probe demands
    what the labels promise: relevant >= 0 > irrelevant.
    """
    status = reranker_status()
    model = _get_model()
    if model is None:
        return {"ok": False, "message": f"Reranker nicht verfügbar (Zustand '{status['state']}')"}
    started = time.monotonic()
    try:
        relevant, irrelevant = model.predict(
            [(_PROBE_QUERY, _PROBE_RELEVANT), (_PROBE_QUERY, _PROBE_IRRELEVANT)]
        )
    except Exception as exc:  # noqa: BLE001
        check = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        served = model.served_models() if isinstance(model, _ApiReranker) else []
        if served and _RERANKER_MODEL_NAME not in served:
            check["hint"] = (
                f"RERANKER_MODEL='{_RERANKER_MODEL_NAME}' kennt der Server nicht. "
                f"Angeboten: {', '.join(served)}"
            )
        elif isinstance(exc, httpx.TimeoutException):
            check["hint"] = (
                f"Keine Antwort in {_RERANKER_API_TIMEOUT:.0f} s – Server im Kaltstart? "
                "RERANKER_API_TIMEOUT erhöhen oder später erneut prüfen (make check)."
            )
        return check
    latency_ms = int((time.monotonic() - started) * 1000)
    ok = float(relevant) >= 0.0 > float(irrelevant)
    scores = f"relevant {float(relevant):.2f} vs. irrelevant {float(irrelevant):.2f}"
    return {
        "ok": ok,
        "message": f"{status['type']}: {scores}" if ok else
        f"{status['type']} unterscheidet nicht: {scores}",
        "hint": "" if ok else
        "Erwartet: passend >= 0 > unpassend. Bei RERANKER_TYPE=api das Prompt-"
        "Template prüfen (RERANKER_API_PROMPT auto/raw), sonst die Score-Skala.",
        "latency_ms": latency_ms,
    }


def _chunk_text(chunk: dict) -> str:
    meta = chunk.get("metadata", {}) or {}
    return meta.get("text") or chunk.get("text", "") or ""


def _unranked(chunks: list[dict], top_n: int) -> list[dict]:
    """Fallback: keep the incoming (hybrid-fused) order. Do NOT pretend these
    are cross-encoder scores – the _relevance_label thresholds are tuned for
    logits in [-10,10], not tiny cosine/RRF values, and would mislabel
    near-zero matches as "relevant", fooling the agent."""
    out = []
    for c in chunks[:top_n]:
        score = c.get("rerank_score", c.get("score", 0.0))
        out.append({
            "chunk_id": c.get("chunk_id")
            or (c.get("metadata") or {}).get("chunk_id", ""),
            "original_score": c.get("original_score", c.get("score", 0.0)),
            "rerank_score": score,
            "relevance_label": "unranked",
            "metadata": c.get("metadata", {}),
        })
    return out


def rerank_chunks(
    query: str,
    chunks: list[dict],
    *,
    top_n: int = 10,
) -> list[dict]:
    """Return the top-n chunks re-ranked by a cross-encoder against the query.

    Each returned chunk has `rerank_score` (cross-encoder logit) and
    `relevance_label`. If the cross-encoder can't load, we preserve the input
    ordering (which is already the hybrid-fused ranking) and copy scores.
    """
    if not chunks:
        return []

    model = _get_model()
    if model is None:
        return _unranked(chunks, top_n)

    pairs = [(query, _chunk_text(c)) for c in chunks]
    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as exc:  # noqa: BLE001
        # Same path as "no model": labelling fused RRF scores with the logit
        # thresholds would call every chunk "relevant". With the API backend
        # this is a network hiccup, not a rare crash, so it must stay honest.
        logger.warning("Reranker predict() failed (%s); keeping fused order", exc)
        return _unranked(chunks, top_n)

    scored = []
    for chunk, score in zip(chunks, scores):
        score = float(score)
        scored.append({
            "chunk_id": chunk.get("chunk_id")
            or (chunk.get("metadata") or {}).get("chunk_id", ""),
            "original_score": chunk.get("original_score", chunk.get("score", 0.0)),
            "rerank_score": score,
            "relevance_label": _relevance_label(score),
            "metadata": chunk.get("metadata", {}),
        })

    scored.sort(key=lambda x: x["rerank_score"], reverse=True)
    return scored[:top_n]

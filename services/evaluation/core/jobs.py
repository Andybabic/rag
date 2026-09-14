"""Asynchronous jobs for the public pipeline API.

The public API is deliberately non-blocking: a caller posts a question, gets a
request id back immediately, and polls for the result. A full agent run takes
tens of seconds to minutes, which is far longer than any sane HTTP client (or
reverse proxy) will hold a connection open.

State lives in Postgres (``api_jobs``), not in process memory, for two reasons:
a caller polling with a request id must still get an answer after the service
restarts, and several evaluation replicas must not each keep their own private
view of the queue.

Execution model: one background loop per process claims queued rows with
``FOR UPDATE SKIP LOCKED`` — so two replicas never run the same job — and hands
each to a task bounded by a semaphore. Agent runs are expensive (many LLM
calls), so the concurrency cap is what keeps a burst of API traffic from
starving the interactive chat that shares the same LLM backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from core.database import get_pool
from core.manager import run_manager
from core.use_cases import (
    get_agent_actions,
    get_default_collection,
    get_system_prompt,
)

logger = logging.getLogger(__name__)

# How many agent runs may execute at once in this process. Two is a deliberate
# floor: the LLM backend is shared with the interactive chat, and a single API
# client should not be able to occupy it.
MAX_CONCURRENCY = int(os.getenv("API_JOB_CONCURRENCY", "2"))
# Fallback poll interval. The normal path is the wake-up event set by
# create_job(); polling only matters for jobs another replica enqueued.
POLL_INTERVAL_S = float(os.getenv("API_JOB_POLL_INTERVAL_S", "5"))
# Hard ceiling on one job. Without it a wedged upstream LLM would hold a
# concurrency slot forever and the queue would quietly stop draining.
JOB_TIMEOUT_S = float(os.getenv("API_JOB_TIMEOUT_S", "900"))
# How many previous turns of the same session are replayed as chat history when
# the caller does not send its own. Bounded: history goes into every prompt.
HISTORY_TURNS = int(os.getenv("API_JOB_HISTORY_TURNS", "10"))

_wakeup = asyncio.Event()
_worker_task: asyncio.Task | None = None
_running: set[asyncio.Task] = set()


# ── job records ───────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _json_field(value: Any, default: Any) -> Any:
    """asyncpg returns JSONB as text unless a codec is registered."""
    if value is None:
        return default
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return value


def job_to_status(row: Any) -> dict:
    """Public status shape — metadata only, never the (large) result payload."""
    return {
        "request_id": str(row["id"]),
        "status": row["status"],
        "use_case": row["use_case"],
        "session_id": str(row["session_id"]),
        "role": row["role"],
        "query": row["query_text"],
        "image_count": len(_json_field(row["images"], [])),
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
        "error": row["error"],
    }


async def create_job(
    *,
    use_case: str,
    session_id: str,
    query: str,
    role: str = "default",
    images: list[str] | None = None,
    history: list[dict] | None = None,
    config: dict | None = None,
    token_id: str | None = None,
) -> dict:
    """Enqueue a job and wake the worker. Returns the public status shape."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO api_jobs (token_id, use_case, session_id, role, status,
                                 query_text, images, history, config)
           VALUES ($1::uuid, $2, $3::uuid, $4, 'queued', $5, $6::jsonb,
                   $7::jsonb, $8::jsonb)
           RETURNING id, token_id, use_case, session_id, role, status,
                     query_text, images, history, config, result, error,
                     created_at, started_at, finished_at""",
        token_id,
        use_case,
        session_id,
        role,
        query,
        json.dumps(images or []),
        json.dumps(history) if history is not None else None,
        json.dumps(config or {}),
    )
    _wakeup.set()
    return job_to_status(row)


async def get_job(job_id: str) -> Any | None:
    pool = await get_pool()
    return await pool.fetchrow(
        """SELECT id, token_id, use_case, session_id, role, status, query_text,
                  images, history, config, result, error, created_at,
                  started_at, finished_at
           FROM api_jobs WHERE id = $1::uuid""",
        job_id,
    )


async def get_job_result(row: Any) -> dict:
    return _json_field(row["result"], {})


# ── worker ────────────────────────────────────────────────────


async def _claim_next_job() -> Any | None:
    """Atomically take the oldest queued job. ``None`` if the queue is empty.

    ``SKIP LOCKED`` is what makes this safe to run in several replicas at once:
    a row already being claimed elsewhere is stepped over instead of blocking.
    """
    pool = await get_pool()
    return await pool.fetchrow(
        """UPDATE api_jobs SET status = 'running', started_at = NOW()
           WHERE id = (
               SELECT id FROM api_jobs
               WHERE status = 'queued'
               ORDER BY created_at
               FOR UPDATE SKIP LOCKED
               LIMIT 1
           )
           RETURNING id, token_id, use_case, session_id, role, status,
                     query_text, images, history, config, result, error,
                     created_at, started_at, finished_at"""
    )


async def _load_session_history(session_id: str, exclude_job_id: str) -> list[dict]:
    """Replay earlier turns of this session as chat history.

    Used when the caller sends no explicit history — a client that only tracks
    the session id still gets a real conversation instead of isolated one-shot
    questions.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT query_text, answer_text FROM queries
           WHERE session_id = $1::uuid AND id <> $2::uuid
           ORDER BY created_at DESC LIMIT $3""",
        session_id,
        exclude_job_id,
        HISTORY_TURNS,
    )
    history: list[dict] = []
    for row in reversed(rows):  # oldest first
        history.append({"role": "user", "content": row["query_text"] or ""})
        history.append({"role": "assistant", "content": row["answer_text"] or ""})
    return history


async def _persist_query(job_id: str, row: Any, result: dict) -> None:
    """Write the finished turn into ``queries``.

    This is what makes an API conversation readable through the same chat /
    metrics / feedback machinery as the web UI: the job id is reused as the
    query id, so request id, transcript entry and feedback all line up.
    """
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO queries (id, use_case, session_id, role, query_text,
                                answer_text, agent_steps, citations, images,
                                sufficient, scores)
           VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7::jsonb, $8::jsonb,
                   $9::jsonb, $10, $11::jsonb)
           ON CONFLICT (id) DO NOTHING""",
        job_id,
        row["use_case"],
        str(row["session_id"]),
        row["role"],
        row["query_text"],
        result.get("answer", ""),
        json.dumps(result.get("agent_steps", [])),
        json.dumps(result.get("citations", [])),
        json.dumps(_json_field(row["images"], [])),
        result.get("sufficient", False),
        json.dumps(result.get("audit", {}) or {}),
    )


async def _finish_job(job_id: str, result: dict) -> None:
    pool = await get_pool()
    await pool.execute(
        """UPDATE api_jobs SET status = 'done', result = $2::jsonb,
                               finished_at = NOW()
           WHERE id = $1::uuid""",
        job_id,
        json.dumps(result),
    )


async def _fail_job(job_id: str, message: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """UPDATE api_jobs SET status = 'error', error = $2, finished_at = NOW()
           WHERE id = $1::uuid""",
        job_id,
        message[:4000],
    )


async def _run_job(row: Any) -> None:
    job_id = str(row["id"])
    try:
        system_prompt = await get_system_prompt(row["use_case"], row["role"])
        available_actions = get_agent_actions(row["use_case"])
        get_default_collection(row["use_case"])  # raises for unknown use cases

        config = _json_field(row["config"], {})
        # Explicit choice only; otherwise SEARCH covers the whole use case.
        collection = config.get("collection") or ""
        filters = config.get("filters") or {}

        history = _json_field(row["history"], None)
        if history is None:
            history = await _load_session_history(str(row["session_id"]), job_id)

        result = await asyncio.wait_for(
            run_manager(
                query=row["query_text"],
                use_case=row["use_case"],
                session_id=str(row["session_id"]),
                use_case_prompt=system_prompt,
                available_actions=available_actions,
                collection=collection,
                filters=filters,
                history=history,
                images=_json_field(row["images"], []),
            ),
            timeout=JOB_TIMEOUT_S,
        )
        result = {**result, "request_id": job_id}

        # Best-effort: a transcript that cannot be written must not turn an
        # answered question into a failed job.
        try:
            await _persist_query(job_id, row, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to log API job %s to queries: %s", job_id, exc)

        await _finish_job(job_id, result)
        logger.info("API job %s finished (use_case=%s)", job_id, row["use_case"])
    except asyncio.TimeoutError:
        logger.warning("API job %s timed out after %.0fs", job_id, JOB_TIMEOUT_S)
        await _fail_job(job_id, f"Zeitüberschreitung nach {JOB_TIMEOUT_S:.0f}s")
    except asyncio.CancelledError:
        # Shutdown: leave the job runnable rather than reporting a failure the
        # caller cannot act on. Best-effort only — if the loop is already tearing
        # down, requeue_stale_jobs() on the next start catches it anyway.
        try:
            await requeue_job(job_id)
        except Exception:  # noqa: BLE001
            logger.warning("Could not requeue cancelled API job %s", job_id)
        raise
    except Exception as exc:  # noqa: BLE001 — any failure belongs to the caller
        logger.exception("API job %s failed", job_id)
        await _fail_job(job_id, f"{type(exc).__name__}: {exc}")


async def requeue_job(job_id: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """UPDATE api_jobs SET status = 'queued', started_at = NULL
           WHERE id = $1::uuid AND status = 'running'""",
        job_id,
    )


async def schema_ready() -> bool:
    """Are the public-API tables present?

    Migrations are applied by hand in this project, so an updated service can
    legitimately meet an older database. Probing beats letting the worker die
    on an UndefinedTableError at startup and taking the whole (otherwise
    healthy) evaluation service down with it.
    """
    pool = await get_pool()
    row = await pool.fetchrow("SELECT to_regclass('public.api_jobs') AS table_name")
    return row is not None and row["table_name"] is not None


async def requeue_stale_jobs() -> int:
    """Return jobs left ``running`` by a crashed process to the queue.

    Nothing is executing them any more, so without this they would poll as
    "running" forever.
    """
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE api_jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
    )
    count = int(result.rsplit(" ", 1)[-1]) if result.startswith("UPDATE") else 0
    if count:
        logger.info("Requeued %d stale API job(s) after restart", count)
    return count


async def _worker_loop() -> None:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    while True:
        await semaphore.acquire()
        try:
            row = await _claim_next_job()
        except Exception as exc:  # noqa: BLE001 — DB blip must not kill the loop
            semaphore.release()
            logger.warning("Could not claim API job: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_S)
            continue

        if row is None:
            semaphore.release()
            try:
                await asyncio.wait_for(_wakeup.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            _wakeup.clear()
            continue

        task = asyncio.create_task(_run_job(row))
        # Hold a strong reference: asyncio only keeps weak ones, so an
        # unreferenced task can be garbage-collected mid-run.
        _running.add(task)
        task.add_done_callback(_running.discard)
        task.add_done_callback(lambda _t: semaphore.release())


def start_worker() -> None:
    """Start the background job worker (idempotent)."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_worker_loop())
    logger.info("API job worker started (concurrency=%d)", MAX_CONCURRENCY)


async def stop_worker() -> None:
    """Stop the worker and put anything still in flight back on the queue."""
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    # Snapshot first: each task's done-callback removes it from the set, so
    # iterating the live set while it drains would skip entries.
    in_flight = list(_running)
    for task in in_flight:
        task.cancel()
    if in_flight:
        await asyncio.gather(*in_flight, return_exceptions=True)

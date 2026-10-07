"""Thin client for the parts of Kermit the benchmark drives.

Ingestion goes through the frontend (``/api/ingest``) so the full production
path runs: cleaning → structure → embed → upsert → ingest log. Use-case setup
and queries talk to the evaluation service directly; ``/v1/agent/query``
answers synchronously and returns the whole trace (chunk pool, compliance).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
from settings import Settings

AGENT_ACTIONS = ["SEARCH", "REFINE_QUERY", "FINAL_ANSWER"]

# Where the frontend runs locally: Vite dev server (make dev) or the
# production build (make up). Tried in this order when no URL is configured.
FRONTEND_CANDIDATES = ["http://localhost:5173", "http://localhost:3000"]


class KermitError(RuntimeError):
    pass


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("detail") or body.get("error") or body)[:300]
    except ValueError:
        return resp.text[:300]


class Kermit:
    def __init__(self, settings: Settings):
        self.s = settings
        self.uc = settings.use_case
        self.eval = httpx.AsyncClient(base_url=settings.evaluation_url, timeout=60.0)
        self.vdb = httpx.AsyncClient(base_url=settings.vectordb_url, timeout=60.0)
        self.cleaning = httpx.AsyncClient(base_url=settings.cleaning_url, timeout=120.0)
        self._use_frontend(settings.frontend_url or FRONTEND_CANDIDATES[0])

    def _use_frontend(self, url: str) -> None:
        self.frontend = url.rstrip("/")
        # SvelteKit rejects form POSTs without a matching Origin (CSRF).
        self.fe = httpx.AsyncClient(
            base_url=self.frontend, timeout=600.0, headers={"origin": self.frontend}
        )

    async def aclose(self) -> None:
        for client in (self.eval, self.vdb, self.cleaning, self.fe):
            await client.aclose()

    # ── health ──────────────────────────────────────────────

    async def check(self) -> None:
        for name, client in (("evaluation", self.eval), ("vectordb", self.vdb)):
            try:
                resp = await client.get("/health")
            except httpx.HTTPError as exc:
                raise KermitError(
                    f"{name} nicht erreichbar unter {client.base_url} ({exc}). Läuft `make dev`?"
                ) from exc
            if resp.status_code >= 500:
                raise KermitError(f"{name} meldet {resp.status_code}: {_detail(resp)}")
        await self._find_frontend()

    async def _find_frontend(self) -> None:
        """Use the configured frontend URL, or the first local one that answers."""
        configured = self.s.frontend_url.rstrip("/")
        candidates = [configured] if configured else FRONTEND_CANDIDATES
        tried = []
        for url in candidates:
            if url != self.frontend:
                await self.fe.aclose()
                self._use_frontend(url)
            try:
                resp = await self.fe.get("/api/health")
            except httpx.HTTPError as exc:
                tried.append(f"{url} ({type(exc).__name__})")
                continue
            if resp.status_code < 500:
                return
            tried.append(f"{url} (HTTP {resp.status_code})")
        raise KermitError(
            f"Frontend nicht erreichbar: {', '.join(tried)}. Läuft `make dev`? "
            "Sonst KERMIT_FRONTEND_URL in benchmark/.env setzen."
        )

    # ── use case ────────────────────────────────────────────

    async def upsert_use_case(self, prompt: str) -> None:
        body = {
            "slug": self.uc,
            "label": "XRAG Benchmark",
            "description": "Automatischer Benchmark (AmazonScience/XRAG, DE/EN)",
            "color": "bg-slate-600",
            "accent": "#475569",
            "roles": ["default"],
            # No RECALL_MEMORY/CLARIFY: memory would carry answers from one
            # question into the next, and a clarifying question can't be graded.
            "agent_action_names": AGENT_ACTIONS,
            "default_collection": f"{self.uc}_default",
            "collection_prefixes": [f"{self.uc}_"],
            "enabled": True,
            "prompts": {"default": prompt},
        }
        resp = await self.eval.put(f"/v1/admin/use-cases/{self.uc}", json=body)
        if resp.status_code >= 400:
            raise KermitError(f"Use Case anlegen fehlgeschlagen: {_detail(resp)}")

    async def list_documents(self) -> list[dict]:
        resp = await self.eval.get("/v1/admin/documents", params={"use_case": self.uc})
        resp.raise_for_status()
        return resp.json().get("documents", [])

    async def indexed_points(self) -> int:
        """Vectors in the use case's document collections (not its memory)."""
        resp = await self.vdb.get("/v1/collections")
        resp.raise_for_status()
        return sum(
            c.get("count") or 0
            for c in resp.json().get("collections", [])
            if c["name"].startswith(f"{self.uc}_")
        )

    async def wipe(self) -> dict:
        """Remove all vectors, memory and ingest-log rows of the benchmark use case.

        The use case itself stays; stored originals are content-addressed and
        reused on the next upload.
        """
        resp = await self.vdb.get("/v1/collections")
        resp.raise_for_status()
        names = [c["name"] for c in resp.json().get("collections", [])]
        dropped = [n for n in names if n.startswith(f"{self.uc}_") or n == f"mem_{self.uc}"]
        for name in dropped:
            (await self.vdb.delete(f"/v1/collection/{name}")).raise_for_status()
        hashes = [d["file_hash"] for d in await self.list_documents() if d.get("file_hash")]
        for i in range(0, len(hashes), 1000):
            resp = await self.eval.post(
                "/v1/ingest-log/delete",
                json={"file_hashes": hashes[i : i + 1000], "use_case": self.uc},
            )
            resp.raise_for_status()
        return {"collections": dropped, "documents": len(hashes)}

    async def remove(self) -> dict:
        """Remove everything the benchmark put into Kermit.

        Vectors, memory and ingest log (``wipe``), the stored originals and
        the use case. What stays: the query log (``queries`` table, the
        traces behind the report's request_ids) and the use case's prompt row.
        """
        info = await self.wipe()
        try:
            resp = await self.cleaning.delete(f"/v1/transfer/{self.uc}")
            resp.raise_for_status()
            info["files"] = resp.json().get("removed", 0)
        except httpx.HTTPError as exc:
            info["files_error"] = f"{type(exc).__name__}: {exc}"
        resp = await self.eval.delete(f"/v1/admin/use-cases/{self.uc}")
        if resp.status_code not in (200, 404):
            raise KermitError(f"Use Case löschen fehlgeschlagen: {_detail(resp)}")
        info["use_case_deleted"] = resp.status_code == 200
        return info

    async def clear_memory(self) -> None:
        """Drop the memory-RAG collection Kermit fills after every answer."""
        resp = await self.vdb.delete(f"/v1/collection/mem_{self.uc}")
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    # ── ingest ──────────────────────────────────────────────

    async def login(self) -> None:
        if not self.s.username or not self.s.password:
            raise KermitError(
                "Kein Login: KERMIT_USERNAME/KERMIT_PASSWORD in benchmark/.env "
                "oder ADMIN_USERNAME/ADMIN_PASSWORD in .env setzen."
            )
        resp = await self.fe.post(
            "/login",
            data={"username": self.s.username, "password": self.s.password},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        if "session" not in self.fe.cookies:
            raise KermitError(f"Login am Frontend fehlgeschlagen (HTTP {resp.status_code}).")

    async def ingest(self, path: Path) -> dict:
        resp = await self.fe.post(
            "/api/ingest",
            data={"use_case": self.uc},
            files={"file": (path.name, path.read_bytes(), "text/markdown")},
        )
        if resp.status_code >= 400:
            raise KermitError(f"HTTP {resp.status_code}: {_detail(resp)}")
        return resp.json()

    # ── query ───────────────────────────────────────────────

    async def ask(self, query: str) -> dict:
        resp = await self.eval.post(
            "/v1/agent/query",
            json={
                "query": query,
                "use_case": self.uc,
                # Fresh session per question: no history, no carry-over.
                "session_id": str(uuid.uuid4()),
                "role": "default",
            },
            timeout=self.s.query_timeout,
        )
        if resp.status_code >= 400:
            raise KermitError(f"HTTP {resp.status_code}: {_detail(resp)}")
        return resp.json()


def compact_result(raw: dict) -> dict:
    """The parts of a run_manager result the scoring needs – not the full trace."""
    chunks = raw.get("global_chunks") or []
    audit = raw.get("audit") or {}
    timing = raw.get("timing") or audit.get("timing") or {}
    return {
        "request_id": raw.get("request_id"),
        "answer": raw.get("answer", ""),
        "chunk_files": [(c.get("metadata") or {}).get("file_name", "") for c in chunks],
        "chunk_scores": [c.get("score") for c in chunks],
        "cited_files": [c.get("file_name", "") for c in raw.get("citations") or []],
        "compliance_verdict": (raw.get("compliance") or {}).get("verdict", ""),
        "compliance_issues": (raw.get("compliance") or {}).get("issues", []),
        "sufficient": bool(raw.get("sufficient")),
        "processing_ms": audit.get("processing_ms"),
        "agent_steps": len(raw.get("agent_steps") or []),
        "subagents": len(raw.get("subagents") or []),
        "plan": [
            {"role": st.get("role"), "sub_query": st.get("sub_query")}
            for st in (raw.get("manager_plan") or {}).get("subtasks", [])
        ],
        # Kermit recalls past answers for short queries; the benchmark clears
        # that memory per variant, this flags a recall that still happened.
        "memory_used": any(p.get("id") == "memory" for p in timing.get("phases") or []),
        "model": audit.get("model"),
    }

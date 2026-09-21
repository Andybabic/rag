"""Skill sandbox service — owns the Docker socket so nothing else has to.

This is the only process in the platform that can start containers. The
evaluation service, which is the one handling untrusted LLM output, talks to it
over HTTP and never touches Docker itself; keeping the socket out of that
service is the whole reason this process exists, since the socket is
root-equivalent on the host.

Two responsibilities:

* ``POST /v1/execute`` — run one skill function in a throwaway container
  (see ``runner.py``) and return its result.
* the egress proxy (see ``proxy.py``) — the single network route available to
  a container that declared allowed hosts.

The service is intentionally dumb about *policy*: it does not know what a
skill is, does not read the database, and never decides whether a function may
run. Permission checks and the allowlist itself come from the caller, which
read them from the rows written at import time.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from proxy import EgressProxy
from pydantic import BaseModel, Field
from runner import (
    DEFAULT_MEMORY_MB,
    DEFAULT_TIMEOUT_MS,
    EGRESS_NETWORK,
    MAX_MEMORY_MB,
    MAX_TIMEOUT_MS,
    RUNNER_IMAGE,
    SandboxUnavailable,
    docker_available,
    ensure_network,
    run_function,
)
from shared.errors import handle_error
from shared.logging import setup_logging
from shared.tracing import RequestIDMiddleware

SERVICE_NAME = "sandbox-service"
VERSION = "1.0.0"

setup_logging(SERVICE_NAME, os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

#: A shared secret between evaluation and sandbox. Without it any process that
#: can reach the internal network could ask for arbitrary code to be executed,
#: which would make every other control here decorative.
SANDBOX_TOKEN = os.getenv("SANDBOX_TOKEN", "").strip()

#: How many containers may run at once. Each call is a process plus a
#: container; unbounded concurrency turns a burst of answers into an outage.
MAX_CONCURRENT = int(os.getenv("SKILL_MAX_CONCURRENT", "4") or 4)

proxy = EgressProxy(port=int(os.getenv("SKILL_PROXY_PORT", "8888") or 8888))
_slots = asyncio.Semaphore(MAX_CONCURRENT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await proxy.start()
    if not docker_available():
        logger.error(
            "Docker CLI unavailable — /v1/execute will fail. Check that "
            "/var/run/docker.sock is mounted into this container."
        )
    else:
        await ensure_network()
    try:
        yield
    finally:
        await proxy.stop()


app = FastAPI(
    title="RAG Platform – Skill Sandbox",
    description="Stateless container execution for skill functions",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestIDMiddleware)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return handle_error(exc, request, service=SERVICE_NAME)


class ExecuteRequest(BaseModel):
    """One function call. The caller has already authorised it."""

    language: str = Field(description="'python' or 'javascript'")
    code: str = Field(description="The skill's complete source file")
    function: str = Field(description="Name of the single function to invoke")
    args: list = Field(default_factory=list)
    variables: dict = Field(
        default_factory=dict,
        description="Skill variables, injected as SKILL_VARS and as env vars",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Hosts this function may reach. Empty = no network at all.",
    )
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    memory_mb: int = DEFAULT_MEMORY_MB
    label: str = Field(default="", description="skill.function, for logging only")


@app.get("/health")
async def health():
    return {
        "status": "ok" if docker_available() else "degraded",
        "service": SERVICE_NAME,
        "version": VERSION,
        "docker": docker_available(),
        "runner_image": RUNNER_IMAGE,
        "egress_network": EGRESS_NETWORK,
        "active_egress_grants": proxy.active_grants,
        "max_concurrent": MAX_CONCURRENT,
        "limits": {"max_timeout_ms": MAX_TIMEOUT_MS, "max_memory_mb": MAX_MEMORY_MB},
    }


@app.post("/v1/execute")
async def execute(body: ExecuteRequest, request: Request):
    if SANDBOX_TOKEN:
        presented = request.headers.get("x-sandbox-token", "")
        if presented != SANDBOX_TOKEN:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Ungültiges Sandbox-Token",
                         "kind": "auth"},
            )

    label = body.label or f"{body.function}"
    # The grant exists only for the lifetime of this call. Revoking it in
    # `finally` matters: a token that outlived its container would be a
    # standing hole in the allowlist for whatever picked it up.
    token = proxy.grant(body.allowed_domains, label) if body.allowed_domains else None

    try:
        async with _slots:
            result = await run_function(
                language=body.language,
                code=body.code,
                function=body.function,
                args=body.args,
                variables=body.variables,
                allowed_domains=body.allowed_domains,
                timeout_ms=body.timeout_ms,
                memory_mb=body.memory_mb,
                proxy_token=token,
            )
    except SandboxUnavailable as exc:
        logger.error("Sandbox unavailable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": str(exc), "kind": "unavailable"},
        )
    finally:
        if token:
            proxy.revoke(token)

    return {
        "ok": result.ok,
        "result": result.result,
        "error": result.error,
        "kind": result.kind,
        "stdout": result.stdout,
        "traceback": result.traceback,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }

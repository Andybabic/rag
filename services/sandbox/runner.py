"""Starts one throwaway container per skill-function call.

Everything about the container is decided here and nothing about it survives
the call: it is started with ``--rm``, gets the job on stdin, and is gone by
the time the result is parsed. There is no pool, no warm container and no
shared filesystem, so two executions cannot observe each other — which is what
"stateless" has to mean if it is to be worth anything.

The flags below are the security boundary, not the language runtime:

``--network none``
    The default. A function only gets a network if it declared hosts, and then
    only onto an internal Docker network whose single reachable peer is the
    egress proxy.
``--read-only`` + ``--tmpfs /tmp``
    Nothing written by a skill outlives the call; /tmp is small and noexec.
``--cap-drop ALL`` + ``--security-opt no-new-privileges``
    No capabilities and no way to regain any.
``--memory`` / ``--pids-limit`` / ``--cpus``
    A runaway function is killed by the kernel instead of starving the host.
``--user 10001``
    Never root, even inside the container.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

RUNNER_IMAGE = os.getenv("SKILL_RUNNER_IMAGE", "kermit-skill-runner:latest")
EGRESS_NETWORK = os.getenv("SKILL_EGRESS_NETWORK", "kermit_skill_egress")
#: How the runner container addresses this service's proxy on that network.
PROXY_HOST = os.getenv("SKILL_PROXY_HOST", "sandbox")
PROXY_PORT = int(os.getenv("SKILL_PROXY_PORT", "8888") or 8888)

#: Ceilings an individual function cannot raise. A skill author sets the
#: per-function values; these bound whatever they set.
MAX_TIMEOUT_MS = int(os.getenv("SKILL_MAX_TIMEOUT_MS", "30000") or 30000)
MAX_MEMORY_MB = int(os.getenv("SKILL_MAX_MEMORY_MB", "512") or 512)
DEFAULT_TIMEOUT_MS = 10_000
DEFAULT_MEMORY_MB = 256
CPU_QUOTA = os.getenv("SKILL_CPU_QUOTA", "0.5")
PIDS_LIMIT = os.getenv("SKILL_PIDS_LIMIT", "64")

#: Grace period on top of the function timeout. Container start, reading the
#: job and the kill itself are not the function's budget, but they are also not
#: free. Measured start-to-first-instruction is ~150-300 ms on a warm image, so
#: this is roughly fifteen times the observed overhead — generous enough for a
#: loaded host, small enough that a 2 s function does not keep a user waiting
#: ten seconds before it reports a timeout.
_CONTAINER_OVERHEAD_S = 5.0

_ENTRYPOINTS = {
    "python": ["python", "/runner/entrypoint.py"],
    "javascript": ["node", "/runner/entrypoint.js"],
}


class SandboxUnavailable(RuntimeError):
    """Docker is not reachable from this service."""


@dataclass
class ExecutionResult:
    ok: bool
    result: object = None
    error: str = ""
    kind: str = ""
    stdout: str = ""
    #: Full traceback for the log. Never shown next to the answer.
    traceback: str = ""
    duration_ms: int = 0
    #: True when the container was killed for exceeding its time budget.
    timed_out: bool = False


def docker_available() -> bool:
    return shutil.which("docker") is not None


async def _run(*args: str, timeout: float = 20.0) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise
    return process.returncode or 0, out, err


async def ensure_network() -> bool:
    """Create the internal egress network if it does not exist yet.

    ``--internal`` is the load-bearing flag: containers on this network have no
    route off the host, so the proxy really is the only way out rather than
    merely the configured one.
    """
    if not docker_available():
        return False
    try:
        code, out, _ = await _run("docker", "network", "ls", "--format", "{{.Name}}")
        if code == 0 and EGRESS_NETWORK in out.decode().split():
            return True
        code, _, err = await _run(
            "docker", "network", "create", "--internal", EGRESS_NETWORK
        )
        if code != 0 and b"already exists" not in err:
            logger.warning("Could not create %s: %s", EGRESS_NETWORK, err.decode()[:300])
            return False
        return True
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("Egress network check failed: %s", exc)
        return False


async def run_function(
    *,
    language: str,
    code: str,
    function: str,
    args: list,
    variables: dict,
    allowed_domains: list[str],
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    memory_mb: int = DEFAULT_MEMORY_MB,
    proxy_token: str | None = None,
) -> ExecutionResult:
    """Execute one function in a fresh container and return its result."""
    if language not in _ENTRYPOINTS:
        return ExecutionResult(ok=False, error=f"Unbekannte Sprache {language!r}",
                               kind="language")
    if not docker_available():
        raise SandboxUnavailable(
            "Docker CLI not found in the sandbox container — the socket mount "
            "or the image build is misconfigured."
        )

    timeout_ms = max(500, min(int(timeout_ms or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS))
    memory_mb = max(64, min(int(memory_mb or DEFAULT_MEMORY_MB), MAX_MEMORY_MB))
    name = f"skill-run-{uuid.uuid4().hex[:12]}"

    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--user", "10001:10001",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", f"{memory_mb}m",
        # Without a matching swap limit the kernel lets the container spill past
        # --memory into swap, and the cap stops meaning anything.
        "--memory-swap", f"{memory_mb}m",
        "--cpus", CPU_QUOTA,
        "--pids-limit", PIDS_LIMIT,
        "--env", "HOME=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
    ]

    if allowed_domains and proxy_token:
        proxy_url = f"http://{proxy_token}:x@{PROXY_HOST}:{PROXY_PORT}"
        cmd += [
            "--network", EGRESS_NETWORK,
            "--env", f"HTTP_PROXY={proxy_url}",
            "--env", f"HTTPS_PROXY={proxy_url}",
            "--env", f"http_proxy={proxy_url}",
            "--env", f"https_proxy={proxy_url}",
            # Read by entrypoint.js, which has to install a proxy dispatcher
            # explicitly — Node's fetch ignores the proxy environment.
            "--env", f"SKILL_PROXY_URL={proxy_url}",
            "--env", "NO_PROXY=",
        ]
    else:
        cmd += ["--network", "none"]

    cmd += [RUNNER_IMAGE] + _ENTRYPOINTS[language]

    job = json.dumps(
        {"code": code, "function": function, "args": args, "variables": variables},
        ensure_ascii=False,
    ).encode("utf-8")

    loop = asyncio.get_running_loop()
    started = loop.time()
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    budget = timeout_ms / 1000.0 + _CONTAINER_OVERHEAD_S
    try:
        out, err = await asyncio.wait_for(process.communicate(job), timeout=budget)
    except asyncio.TimeoutError:
        # `docker run` blocking past the budget means the container is still
        # alive; killing the client alone would leave it running.
        await _force_remove(name)
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return ExecutionResult(
            ok=False,
            error=f"Zeitlimit von {timeout_ms} ms überschritten.",
            kind="timeout",
            timed_out=True,
            duration_ms=int((loop.time() - started) * 1000),
        )

    duration_ms = int((loop.time() - started) * 1000)
    payload = out.decode("utf-8", "replace").strip()
    stderr_text = err.decode("utf-8", "replace").strip()

    if not payload:
        # OOM kills and image-pull failures both land here, and they need
        # different fixes, so name them rather than reporting "no output".
        if process.returncode == 137:
            message = f"Speicherlimit von {memory_mb} MB überschritten."
            kind = "memory"
        else:
            message = stderr_text[:500] or "Der Container lieferte kein Ergebnis."
            kind = "container"
        return ExecutionResult(ok=False, error=message, kind=kind,
                               duration_ms=duration_ms)

    try:
        parsed = json.loads(payload)
    except ValueError:
        return ExecutionResult(
            ok=False,
            error=f"Unlesbare Antwort aus dem Container: {payload[:300]}",
            kind="protocol",
            duration_ms=duration_ms,
        )

    if not parsed.get("ok"):
        return ExecutionResult(
            ok=False,
            error=str(parsed.get("error") or "Unbekannter Fehler"),
            kind=str(parsed.get("kind") or "runtime"),
            traceback=str(parsed.get("traceback") or ""),
            duration_ms=duration_ms,
        )
    return ExecutionResult(
        ok=True,
        result=parsed.get("result"),
        stdout=str(parsed.get("stdout") or ""),
        duration_ms=duration_ms,
    )


async def _force_remove(name: str) -> None:
    try:
        await _run("docker", "rm", "-f", name, timeout=10.0)
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("Could not force-remove container %s: %s", name, exc)

"""The plugin layer's pipeline step: find skill calls, gate them, run them.

Runs once per answer, after the synthesiser and compliance are done and before
the answer goes on the wire. The order is fixed and each stage is allowed to
drop a call:

1. **Parse.** Find every ``skillname.funktion(args)`` in the answer text.
   Candidates naming a skill this use case does not have are discarded here.
2. **Resolve.** Look each one up in the database. A call that does not resolve
   to an enabled skill with a code file and that exact function is *never*
   executed — it is marked in the answer as unknown. Nothing runs on suspicion.
3. **Consent.** Functions whose row carries ``ask_for_permission`` stop and ask
   the user. Denial and timeout both mean "do not run".
4. **Execute.** The sandbox service starts one throwaway container per call.
5. **Substitute.** Each call's text is replaced by its result, or by a visible
   error marker. A failure here can never fail the answer.

The skill's variables are read in step 4 and handed straight to the sandbox.
They are not logged, not returned to the frontend, and not placed anywhere the
LLM can see them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from shared.skills import FunctionCall, find_calls, substitute
from shared.usecase_config import (
    get_execution_bundle,
    list_skill_functions,
    log_skill_execution,
)

from core import permissions

logger = logging.getLogger(__name__)

SANDBOX_URL = os.getenv("SANDBOX_SERVICE_URL", "http://sandbox:8007").rstrip("/")
SANDBOX_TOKEN = os.getenv("SANDBOX_TOKEN", "").strip()

#: Master switch. Off means the parser never runs and no container can start,
#: which is what a deployment without a sandbox container needs.
ENABLED = os.getenv("SKILL_FUNCTIONS_ENABLED", "true").lower() not in ("0", "false", "no")

PERMISSION_TIMEOUT_S = float(os.getenv("SKILL_PERMISSION_TIMEOUT_S", "120") or 120)

EventCallback = Callable[[dict], Awaitable[None]] | None


@dataclass
class CallOutcome:
    """What happened to one recognised call, for the trace and the audit."""

    skill: str
    function: str
    args: list
    status: str  # ok | error | denied | timeout | unresolved
    rendered: str
    detail: str = ""
    duration_ms: int = 0
    permission_required: bool = False


def _render_result(value: Any) -> str:
    """Turn a function's return value into text that fits inside an answer.

    Scalars go in verbatim — a temperature should read as ``14 °C``, not as
    ``"14 °C"``. Structures are compact JSON, which is at least legible and
    keeps the model honest about what came back.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    except (TypeError, ValueError):
        return str(value)


def _render_error(call: FunctionCall, detail: str) -> str:
    """A failed call stays visible. Silently deleting it would leave a sentence
    that reads as fact with nothing behind it."""
    return f"⚠️ {call.skill}.{call.function} — {detail}"


async def _call_sandbox(bundle, args: list) -> dict:
    payload = {
        "language": bundle.language,
        "code": bundle.code,
        "function": bundle.function.name,
        "args": args,
        "variables": bundle.variables,
        "allowed_domains": bundle.function.allowed_domains,
        "timeout_ms": bundle.function.timeout_ms,
        "memory_mb": bundle.function.memory_mb,
        "label": bundle.function.qualified_name,
    }
    headers = {"X-Sandbox-Token": SANDBOX_TOKEN} if SANDBOX_TOKEN else {}
    # The sandbox enforces the function's own timeout and kills the container;
    # this one only covers the case where the sandbox itself stops answering.
    budget = bundle.function.timeout_ms / 1000.0 + 20.0
    async with httpx.AsyncClient(timeout=budget) as client:
        response = await client.post(
            f"{SANDBOX_URL}/v1/execute", json=payload, headers=headers
        )
        response.raise_for_status()
        return response.json()


async def _resolve(call: FunctionCall, use_case: str):
    """Look one call up. Returns the bundle, or None when it does not resolve.

    Separate from execution so the whole answer's calls can be resolved at once:
    the permission flags decide which calls may run concurrently, and that is
    not knowable before the lookup.
    """
    return await get_execution_bundle(use_case, call.skill, call.function)


async def _execute_one(
    call: FunctionCall,
    bundle,
    *,
    use_case: str,
    session_id: str,
    request_id: str | None,
    username: str | None,
    on_event: EventCallback,
) -> CallOutcome:
    args = list(call.args)

    if bundle is None:
        # Resolution failed: unknown skill, unknown function, skill disabled, or
        # a skill with no code file. All four mean the same thing here.
        await log_skill_execution(
            use_case=use_case, skill_name=call.skill, function_name=call.function,
            status="unresolved", session_id=session_id, request_id=request_id,
            username=username, args=args,
            error="Skill oder Funktion nicht gefunden",
        )
        return CallOutcome(
            skill=call.skill, function=call.function, args=args, status="unresolved",
            rendered=_render_error(call, "unbekannte Funktion"),
            detail="Skill oder Funktion existiert nicht für diesen Use Case.",
        )

    fn = bundle.function

    if fn.ask_for_permission:
        pending = permissions.register(
            session_id=session_id, use_case=use_case, skill=bundle.skill_name,
            function=fn.name, args=call.args, description=fn.description,
        )
        if on_event:
            await on_event({
                "type": "permission_request",
                "call_id": pending.call_id,
                "skill": bundle.skill_name,
                "function": fn.name,
                "signature": fn.signature,
                "description": fn.description,
                "args": args,
                "allowed_domains": fn.allowed_domains,
                "timeout_s": PERMISSION_TIMEOUT_S,
            })
        else:
            # No event channel means no way to ask — and an unasked question is
            # not consent. Non-streaming callers (the public API) therefore
            # never run permission-gated functions.
            permissions.cancel(pending.call_id)
            await log_skill_execution(
                use_case=use_case, skill_id=bundle.skill_id,
                skill_name=bundle.skill_name, function_name=fn.name,
                status="denied", session_id=session_id, request_id=request_id,
                username=username, args=args, permission_required=True,
                permission_granted=False,
                error="Kein Kanal für die Rückfrage verfügbar",
            )
            return CallOutcome(
                skill=bundle.skill_name, function=fn.name, args=args, status="denied",
                rendered=_render_error(call, "Freigabe nicht möglich"),
                detail="Diese Funktion benötigt eine Freigabe, die hier nicht "
                       "eingeholt werden kann.",
                permission_required=True,
            )

        approved = await permissions.wait_for(pending, timeout=PERMISSION_TIMEOUT_S)
        if on_event:
            await on_event({
                "type": "permission_resolved",
                "call_id": pending.call_id,
                "approved": approved,
            })
        if not approved:
            await log_skill_execution(
                use_case=use_case, skill_id=bundle.skill_id,
                skill_name=bundle.skill_name, function_name=fn.name,
                status="denied", session_id=session_id, request_id=request_id,
                username=username, args=args, permission_required=True,
                permission_granted=False,
            )
            return CallOutcome(
                skill=bundle.skill_name, function=fn.name, args=args, status="denied",
                rendered=_render_error(call, "nicht freigegeben"),
                detail="Der Nutzer hat die Ausführung nicht freigegeben.",
                permission_required=True,
            )

    if on_event:
        await on_event({
            "type": "function_started",
            "skill": bundle.skill_name,
            "function": fn.name,
            "args": args,
        })

    try:
        response = await _call_sandbox(bundle, args)
    except httpx.HTTPError as exc:
        detail = f"Sandbox nicht erreichbar: {exc.__class__.__name__}"
        logger.warning("Sandbox call %s failed: %s", fn.qualified_name, exc)
        await log_skill_execution(
            use_case=use_case, skill_id=bundle.skill_id,
            skill_name=bundle.skill_name, function_name=fn.name, status="error",
            session_id=session_id, request_id=request_id, username=username,
            args=args, permission_required=fn.ask_for_permission,
            permission_granted=True if fn.ask_for_permission else None,
            error=str(exc)[:500],
        )
        return CallOutcome(
            skill=bundle.skill_name, function=fn.name, args=args, status="error",
            rendered=_render_error(call, "Sandbox nicht erreichbar"), detail=detail,
            permission_required=fn.ask_for_permission,
        )

    duration_ms = int(response.get("duration_ms") or 0)
    if response.get("ok"):
        rendered = _render_result(response.get("result"))
        await log_skill_execution(
            use_case=use_case, skill_id=bundle.skill_id,
            skill_name=bundle.skill_name, function_name=fn.name, status="ok",
            session_id=session_id, request_id=request_id, username=username,
            args=args, permission_required=fn.ask_for_permission,
            permission_granted=True if fn.ask_for_permission else None,
            result_bytes=len(rendered.encode("utf-8")), duration_ms=duration_ms,
        )
        outcome = CallOutcome(
            skill=bundle.skill_name, function=fn.name, args=args, status="ok",
            rendered=rendered, duration_ms=duration_ms,
            permission_required=fn.ask_for_permission,
        )
    else:
        error = str(response.get("error") or "Unbekannter Fehler")
        status = "timeout" if response.get("timed_out") else "error"
        short = "Zeitlimit überschritten" if status == "timeout" else error.split("\n")[0][:120]
        # The traceback is for whoever wrote the skill, not for whoever asked
        # the question: it goes to the log, while the answer shows `short`.
        if response.get("traceback"):
            logger.info(
                "Skill %s failed:\n%s", fn.qualified_name, response["traceback"]
            )
        await log_skill_execution(
            use_case=use_case, skill_id=bundle.skill_id,
            skill_name=bundle.skill_name, function_name=fn.name, status=status,
            session_id=session_id, request_id=request_id, username=username,
            args=args, permission_required=fn.ask_for_permission,
            permission_granted=True if fn.ask_for_permission else None,
            error=error, duration_ms=duration_ms,
        )
        outcome = CallOutcome(
            skill=bundle.skill_name, function=fn.name, args=args, status=status,
            rendered=_render_error(call, short), detail=error,
            duration_ms=duration_ms, permission_required=fn.ask_for_permission,
        )

    if on_event:
        await on_event({
            "type": "function_result",
            "skill": outcome.skill,
            "function": outcome.function,
            "status": outcome.status,
            "preview": outcome.rendered[:300],
            "duration_ms": outcome.duration_ms,
        })
    return outcome


async def execute_named_function(
    skill: str,
    function: str,
    args: list,
    *,
    use_case: str,
    session_id: str,
    request_id: str | None = None,
    username: str | None = None,
    on_event: EventCallback = None,
) -> str:
    """Run one function by name and render the result as an observation.

    Used by the ``SKILL`` action. Shares ``_execute_one`` with the
    text-pattern path, so resolution, the consent gate, the sandbox call and
    the audit row behave identically no matter which way the agent got here —
    a second execution path with its own idea of when to ask permission is
    exactly the kind of thing that ends up not asking.
    """
    if not ENABLED:
        return "Skill-Funktionen sind in dieser Installation deaktiviert."

    # A synthetic call: _execute_one works on spans of answer text, and here
    # there is no answer yet. The span is never used because nothing is
    # substituted — the result goes back as an OBSERVATION instead.
    call = FunctionCall(
        skill=skill, function=function, args=tuple(args),
        start=0, end=0, raw=f"{skill}.{function}(…)",
    )
    outcome = await _execute_one(
        call, await _resolve(call, use_case),
        use_case=use_case, session_id=session_id,
        request_id=request_id, username=username, on_event=on_event,
    )

    if outcome.status == "ok":
        return outcome.rendered
    if outcome.status == "unresolved":
        known = ", ".join(
            sorted(f.qualified_name for f in await list_skill_functions(use_case))
        )
        return (
            f"Fehler: {skill}.{function} gibt es nicht. "
            f"Verfügbar: {known or '(keine)'}"
        )
    if outcome.status == "denied":
        return (
            f"Der Nutzer hat {skill}.{function} nicht freigegeben. Führe die "
            "Funktion nicht erneut aus und antworte ohne diesen Wert."
        )
    return f"Fehler bei {skill}.{function}: {outcome.detail or outcome.status}"


async def apply_skill_functions(
    answer: str,
    *,
    use_case: str,
    session_id: str,
    request_id: str | None = None,
    username: str | None = None,
    on_event: EventCallback = None,
) -> tuple[str, list[dict]]:
    """Execute every resolvable skill call in *answer* and substitute results.

    Returns the rewritten answer and one trace dict per recognised call. On any
    unexpected failure the original answer is returned unchanged — a broken
    plugin layer must never cost the user an answer that is already finished.
    """
    if not ENABLED or not answer:
        return answer, []

    try:
        functions = await list_skill_functions(use_case)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load skill functions for %s: %s", use_case, exc)
        return answer, []

    if not functions:
        return answer, []

    known = {f.skill_name.lower() for f in functions}
    calls = find_calls(answer, known_skills=known)
    if not calls:
        return answer, []

    logger.info(
        "Skill plugin layer: %d call(s) across %d skill(s) recognised in %s answer",
        len(calls), len({c.skill.lower() for c in calls}), use_case,
    )

    # Resolve everything first, concurrently. Two reasons: the lookups are
    # independent, and the permission flag they return is what decides which
    # calls are allowed to run together below.
    resolved = await asyncio.gather(
        *(_resolve(call, use_case) for call in calls), return_exceptions=True
    )

    async def run(index: int) -> CallOutcome:
        call = calls[index]
        bundle = resolved[index]
        try:
            if isinstance(bundle, BaseException):
                raise bundle
            return await _execute_one(
                call, bundle, use_case=use_case, session_id=session_id,
                request_id=request_id, username=username, on_event=on_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Skill call %s failed unexpectedly", call.label())
            return CallOutcome(
                skill=call.skill, function=call.function, args=list(call.args),
                status="error", rendered=_render_error(call, "interner Fehler"),
                detail=str(exc)[:300],
            )

    # Group the calls into batches. A call that needs consent is its own batch:
    # two consent dialogs open at once in a single chat bubble is not something
    # a user can reason about, and the answer to one may well be "no". Anything
    # that runs without asking goes in with its neighbours — a use case with
    # several skills routinely produces two or three calls per answer, and
    # running those strictly one after another spends seconds of the user's
    # wait on containers that could have started together.
    batches: list[list[int]] = []
    automatic: list[int] = []
    for index, bundle in enumerate(resolved):
        needs_consent = (
            not isinstance(bundle, BaseException)
            and bundle is not None
            and bundle.function.ask_for_permission
        )
        if needs_consent:
            if automatic:
                batches.append(automatic)
                automatic = []
            batches.append([index])
        else:
            automatic.append(index)
    if automatic:
        batches.append(automatic)

    outcomes: dict[int, CallOutcome] = {}
    for batch in batches:
        if len(batch) == 1:
            outcomes[batch[0]] = await run(batch[0])
        else:
            for index, outcome in zip(batch, await asyncio.gather(*(run(i) for i in batch))):
                outcomes[index] = outcome

    replacements: list[tuple[FunctionCall, str]] = []
    traces: list[dict] = []
    for index, call in enumerate(calls):
        outcome = outcomes[index]
        replacements.append((call, outcome.rendered))
        traces.append({
            "skill": outcome.skill,
            "function": outcome.function,
            "args": outcome.args,
            "status": outcome.status,
            "result_preview": outcome.rendered[:300],
            "detail": outcome.detail,
            "duration_ms": outcome.duration_ms,
            "permission_required": outcome.permission_required,
        })

    return substitute(answer, replacements), traces


def format_functions_block(functions: list) -> str:
    """The prompt section that tells the model these functions exist.

    Teaches the ``SKILL`` action as the primary route: the result comes back as
    an OBSERVATION the model can read, combine with document chunks and cite.
    The text-pattern form is mentioned second and deliberately briefly — it
    still works, but by the time it is substituted the answer is already
    written, so the model cannot react to what came back.

    Functions are grouped under their skill with the skill's own summary: a use
    case can carry several, and a flat list of signatures makes the model guess
    which one a question belongs to.
    """
    if not functions:
        return ""

    by_skill: dict[str, list] = {}
    for fn in functions:
        by_skill.setdefault(fn.skill_name, []).append(fn)

    lines = [
        "# Ausführbare Skill-Funktionen",
        "",
        "Diese Funktionen liefern Daten, die in KEINEM Dokument stehen können "
        "(Live-Werte, externe Dienste). Wenn die Frage so etwas verlangt, ist "
        "das der richtige Weg — nicht die Dokumentensuche.",
        "",
        "**So rufst du sie auf:**",
        "",
        "```",
        'ACTION: SKILL({"skill": "<skill>", "function": "<funktion>", '
        '"args": ["<wert aus der Frage>"]})',
        "```",
        "",
        "Die Argumente kommen **aus der Frage des Nutzers** — übernimm niemals "
        "Werte aus diesem Beispiel oder aus einer Beschreibung.",
        "",
        "Das Ergebnis kommt als OBSERVATION zurück. Verwende es dann in deiner "
        "FINAL_ANSWER und nenne die Herkunft im Satz (z. B. „laut Skill "
        "wetter“). Setze dahinter **kein** [n]: Chunk-Nummern verweisen auf "
        "Dokumente, und kein Dokument belegt einen Live-Wert.",
        "",
        "Regeln:",
        "- Erfinde **niemals** selbst einen Wert, den eine Funktion liefern kann.",
        "- `args` ist eine Liste in der Reihenfolge der Parameter.",
        "- Nur die unten gelisteten Funktionen existieren.",
    ]
    if len(by_skill) > 1:
        lines.append(
            "- Achte auf den Skill-Namen: benutze nur Funktionen des Skills, "
            "der zur Frage passt."
        )
    lines += [
        "",
        "Verfügbar:",
        "",
    ]

    for skill_name, fns in by_skill.items():
        overview = (fns[0].skill_overview or "").strip()
        lines.append(f"## {skill_name}" + (f" — {overview}" if overview else ""))
        for fn in fns:
            description = f" — {fn.description}" if fn.description else ""
            consent = (
                " (fragt vor der Ausführung nach Freigabe)"
                if fn.ask_for_permission else ""
            )
            params = ", ".join(f'"{p}"' for p in fn.params) or ""
            lines.append(
                f'- `SKILL({{"skill": "{skill_name}", "function": "{fn.name}", '
                f'"args": [{params}]}})`{description}{consent}'
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n\n"

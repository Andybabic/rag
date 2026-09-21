"""Runner entry point (Python) — executes exactly one skill function.

Runs inside the ephemeral container. Reads one JSON job from stdin, writes one
JSON result to stdout, exits. Nothing persists: the container is started with
``--rm`` for this single call and destroyed afterwards.

The job is::

    {"code": "...", "function": "aktuell", "args": ["Nürnberg"],
     "variables": {"api_key": "..."}}

Two things are deliberate here. The module is executed with a ``__builtins__``
that still contains ``open`` and ``__import__`` — the isolation that matters is
the container's (read-only filesystem, no network unless allowlisted, dropped
capabilities, memory and pid caps), not a Python-level sandbox, which is
well known to be escapable and would only give false confidence.

The second is that only the *named* function is called. The module body runs,
because imports and constants live there, but no other function in the file is
invoked and there is no path from the job to a shell.
"""

from __future__ import annotations

import io
import json
import sys
import traceback

#: Anything larger is a bug or an attempt to flood the answer.
MAX_RESULT_BYTES = 64 * 1024


def _fail(message: str, kind: str = "error", trace: str = "") -> None:
    """Report a failure and stop.

    ``error`` is what a user ends up reading next to the failed call in the
    answer, so it has to be the exception's own message — a traceback's first
    line is "Traceback (most recent call last):", which tells nobody anything.
    The traceback goes in its own field for the log.
    """
    payload = {"ok": False, "error": message, "kind": kind}
    if trace:
        payload["traceback"] = trace[:4000]
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.flush()
    raise SystemExit(1)


def _message(exc: BaseException) -> str:
    """A one-line, human-readable form of an exception.

    A skill that raises ``ValueError("Ort 'X' wurde nicht gefunden.")`` has
    written the sentence the user should see; anything without a message falls
    back to the class name so the report is never empty.
    """
    text = str(exc).strip().splitlines()
    first = text[0] if text else ""
    return first[:300] if first else exc.__class__.__name__


def main() -> None:
    try:
        job = json.loads(sys.stdin.read())
    except ValueError as exc:
        _fail(f"Ungültiger Job: {exc}", "protocol")
        return

    code = job.get("code") or ""
    function_name = job.get("function") or ""
    args = list(job.get("args") or [])
    variables = dict(job.get("variables") or {})

    if not function_name:
        _fail("Kein Funktionsname übergeben.", "protocol")

    # A leading underscore marks a private helper, and the import step never
    # writes a database row for one — so a call naming it cannot resolve and
    # never reaches here. Refusing it again is a second lock on the same door:
    # the container's contract is "exactly the one function that was approved",
    # and that should hold even if a caller is wrong.
    if function_name.startswith("_"):
        _fail(
            f"{function_name!r} ist ein privater Helfer und nicht aufrufbar.",
            "private_function",
        )

    # Variables reach the function as a module-level SKILL_VARS dict *and* as
    # environment variables, so a skill author can use whichever reads better.
    import os

    for key, value in variables.items():
        if isinstance(key, str) and key.isidentifier():
            os.environ[key] = "" if value is None else str(value)

    module_globals: dict = {
        "__name__": "skill",
        "__builtins__": __builtins__,
        "SKILL_VARS": variables,
    }

    # print() inside a skill must not corrupt the result protocol, which owns
    # stdout. Capture it and hand it back as a diagnostic field instead.
    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        try:
            exec(compile(code, "<skill>", "exec"), module_globals)
        except Exception as exc:
            sys.stdout = real_stdout
            _fail(
                f"Fehler beim Laden der Skill-Datei: {_message(exc)}",
                "load",
                traceback.format_exc(limit=4),
            )

        target = module_globals.get(function_name)
        if not callable(target):
            sys.stdout = real_stdout
            _fail(f"Funktion {function_name!r} existiert nicht.", "missing_function")

        try:
            result = target(*args)
        except TypeError as exc:
            sys.stdout = real_stdout
            _fail(f"Falsche Argumente für {function_name}: {exc}", "arguments")
        except Exception as exc:
            sys.stdout = real_stdout
            _fail(_message(exc), "runtime", traceback.format_exc(limit=4))
    finally:
        sys.stdout = real_stdout

    try:
        payload = json.dumps(
            {
                "ok": True,
                "result": result,
                "stdout": captured.getvalue()[:2000],
            },
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        _fail(f"Ergebnis ist nicht JSON-serialisierbar: {exc}", "result")
        return

    if len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
        _fail(
            f"Ergebnis überschreitet {MAX_RESULT_BYTES // 1024} KB.", "result_too_large"
        )

    sys.stdout.write(payload)
    sys.stdout.flush()


if __name__ == "__main__":
    main()

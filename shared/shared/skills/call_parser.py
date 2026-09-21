"""Recognise ``skillname.funktion(arg, ...)`` calls in an LLM answer.

This runs over finished model output, so it has to be conservative in both
directions. Missing a call means the user sees a literal ``wetter.aktuell(…)``
in their answer; matching too eagerly means executing something the model
never meant as a call — a file name, a version string, a method mentioned in
prose. The regex therefore only *locates* candidates; every match is then
bounded by a real paren scan and, in the pipeline, checked against the skill
and function rows in the database before anything is executed.

Returned spans are byte-accurate offsets into the original text so the caller
can substitute results back in without re-searching.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: Hard ceilings. A malformed answer must not be able to queue a thousand
#: container starts or hand the sandbox a megabyte-long argument.
MAX_CALLS_PER_ANSWER = 8
MAX_ARGS = 8
MAX_ARG_LENGTH = 512

# Locates `name.name(` with nothing word-like glued to the front, so
# `obj.wetter.aktuell(` and `x=wetter.aktuell(` are treated differently:
# the former is a property chain we do not own, the latter is a call.
_CALL_START_RE = re.compile(
    r"(?<![\w.])([A-Za-z_][A-Za-z0-9_-]{0,63})\.([A-Za-z_][A-Za-z0-9_]{0,63})\s*\(",
)


@dataclass(frozen=True)
class FunctionCall:
    """One syntactically valid call found in the answer text."""

    skill: str
    function: str
    args: tuple[object, ...]
    #: Half-open character span of the whole call in the source text,
    #: including any single backticks the model wrapped it in.
    start: int
    end: int
    #: The matched source, for logging and for the consent prompt.
    raw: str

    def label(self) -> str:
        rendered = ", ".join(_render_arg(a) for a in self.args)
        return f"{self.skill}.{self.function}({rendered})"


def _render_arg(value: object) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _scan_arguments(text: str, open_paren: int) -> tuple[str, int] | None:
    """Return the raw argument text and the index just past the closing paren.

    Walks the string so that parentheses inside quoted arguments — a place
    name like ``"Frankfurt (Oder)"`` — do not close the call early. Returns
    None when the parentheses never balance, which is the normal outcome for
    prose that happens to contain ``word.word(``.
    """
    depth = 0
    i = open_paren
    quote: str | None = None
    escape = False
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1: i], i + 1
        elif ch == "\n" and depth == 1 and i - open_paren > 400:
            # An unterminated call that runs past a paragraph is prose.
            return None
        i += 1
    return None


def _split_top_level(raw: str) -> list[str]:
    """Split an argument list on commas that are not nested or quoted."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    current = ""
    for ch in raw:
        if quote is not None:
            current += ch
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += ch
    if current.strip() or parts:
        parts.append(current)
    return parts


_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _parse_arg(raw: str) -> object:
    """Turn one argument token into a JSON-compatible Python value.

    Bare words become strings: models write ``wetter.aktuell(Nürnberg)`` as
    often as they write it quoted, and rejecting the unquoted form would make
    the feature look broken for exactly the calls users notice most.
    """
    token = raw.strip()
    if not token:
        return ""
    if token[0] in "\"'" and len(token) >= 2 and token[-1] == token[0]:
        inner = token[1:-1]
        if token[0] == '"':
            try:
                return json.loads(token)
            except ValueError:
                pass
        return inner.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
    if token[0] in "[{":
        try:
            return json.loads(token)
        except ValueError:
            return token
    lowered = token.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null"):
        return None
    if _NUMBER_RE.match(token):
        return float(token) if "." in token else int(token)
    return token


def _widen_over_backticks(text: str, start: int, end: int) -> tuple[int, int]:
    """Include wrapping single backticks so substitution leaves no stray marks."""
    if start > 0 and text[start - 1] == "`" and end < len(text) and text[end] == "`":
        return start - 1, end + 1
    return start, end


def find_calls(text: str, *, known_skills: set[str] | None = None) -> list[FunctionCall]:
    """Find every well-formed skill call in *text*.

    When *known_skills* is given, candidates naming anything else are dropped
    immediately — that alone removes almost every false positive from prose,
    since ``config.json(...)`` or ``str.format(...)`` names no skill.
    """
    if not text:
        return []

    calls: list[FunctionCall] = []
    position = 0
    while position < len(text):
        match = _CALL_START_RE.search(text, position)
        if match is None:
            break
        skill, function = match.group(1), match.group(2)
        open_paren = match.end() - 1
        position = match.end()

        if known_skills is not None and skill.lower() not in known_skills:
            continue

        scanned = _scan_arguments(text, open_paren)
        if scanned is None:
            continue
        raw_args, after = scanned

        tokens = _split_top_level(raw_args) if raw_args.strip() else []
        if len(tokens) > MAX_ARGS:
            continue
        args = tuple(_parse_arg(t) for t in tokens)
        if any(isinstance(a, str) and len(a) > MAX_ARG_LENGTH for a in args):
            continue

        start, end = _widen_over_backticks(text, match.start(), after)
        calls.append(FunctionCall(
            skill=skill,
            function=function,
            args=args,
            start=start,
            end=end,
            raw=text[match.start():after],
        ))
        position = after
        if len(calls) >= MAX_CALLS_PER_ANSWER:
            break

    return calls


def substitute(text: str, replacements: list[tuple[FunctionCall, str]]) -> str:
    """Replace each call's span with its rendered result.

    Applied right-to-left so earlier spans keep their offsets. Calls absent
    from *replacements* are left in the text untouched — an unresolved call is
    visible rather than silently deleted.
    """
    ordered = sorted(replacements, key=lambda pair: pair[0].start, reverse=True)
    out = text
    for call, rendered in ordered:
        out = out[:call.start] + rendered + out[call.end:]
    return out

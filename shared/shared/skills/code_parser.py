"""Static analysis of a skill's code file — the import step of the plugin layer.

A skill carries at most one code file (``.py`` **or** ``.js``, never both).
This module reads that source once, at import/save time, and derives the
function table that goes into ``usecase_skill_functions``: which functions
exist, which of them need a user's consent before they run, and which hosts
each one is allowed to reach.

Deriving it once is the point. The permission flag decides whether a user is
asked before code runs, so re-reading it out of the source on every execution
would mean the marker could be edited between the check and the run. The DB
row is the authority; this parser is what writes it.

Markers (recognised on the comment lines directly above a definition, or on
the first comment lines inside its body)::

    # ask for permission          →  ask_for_permission = True   (Python)
    // ask for permission         →  ask_for_permission = True   (JavaScript)
    # allow-network: a.com, b.com →  allowed_domains = ["a.com", "b.com"]

Anything else on those comment lines becomes the function's description — that
is the text the user sees in the consent prompt, so it is worth writing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Languages a skill file may be written in.
LANGUAGES = ("python", "javascript")

#: Extension → language, for callers that only have a file name.
EXTENSION_LANGUAGE = {".py": "python", ".js": "javascript", ".mjs": "javascript"}

# Keep a single skill file from turning into an unbounded prompt/DB payload.
MAX_SOURCE_BYTES = 256 * 1024
MAX_FUNCTIONS = 32


class SkillCodeError(ValueError):
    """The skill's code file could not be parsed into a function table."""


@dataclass(frozen=True)
class ParsedFunction:
    """One callable function discovered in a skill's code file."""

    name: str
    ask_for_permission: bool = False
    allowed_domains: tuple[str, ...] = ()
    description: str = ""
    #: Parameter names, in declaration order — used to render the signature in
    #: the prompt block so the LLM emits arguments in the right order.
    params: tuple[str, ...] = ()
    line: int = 0

    def signature(self) -> str:
        return f"{self.name}({', '.join(self.params)})"


@dataclass
class _Markers:
    ask_for_permission: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    description_lines: list[str] = field(default_factory=list)


# ── marker extraction ────────────────────────────────────────────────────────

_PERMISSION_RE = re.compile(r"^ask\s+for\s+permission\s*$", re.IGNORECASE)
_NETWORK_RE = re.compile(r"^allow-network\s*:\s*(.+)$", re.IGNORECASE)

# A hostname we are willing to hand to the egress proxy as an allowlist entry.
# Deliberately strict: no scheme, no path, no port, no wildcard — the proxy
# compares the CONNECT target against this set verbatim.
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                        r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _strip_comment(line: str, prefix: str) -> str | None:
    """Return the comment body of *line*, or None if it is not a comment."""
    stripped = line.strip()
    if not stripped.startswith(prefix):
        return None
    return stripped[len(prefix):].strip()


def _collect_markers(comment_bodies: list[str]) -> _Markers:
    """Split comment text into markers and free-form description."""
    markers = _Markers()
    for body in comment_bodies:
        if not body:
            continue
        if _PERMISSION_RE.match(body):
            markers.ask_for_permission = True
            continue
        net = _NETWORK_RE.match(body)
        if net:
            for raw in net.group(1).split(","):
                host = raw.strip().lower()
                # Tolerate a pasted URL: keep the host, drop the rest.
                host = re.sub(r"^[a-z]+://", "", host).split("/")[0].split(":")[0]
                if host and _DOMAIN_RE.match(host) and host not in markers.allowed_domains:
                    markers.allowed_domains.append(host)
            continue
        markers.description_lines.append(body)
    return markers


def _comments_above(lines: list[str], index: int, prefix: str) -> list[str]:
    """Comment bodies in the comment block directly above *index*.

    The block must abut the definition: a blank line ends it. Skipping blanks
    would let one function's trailing comment be read as the next function's
    marker — and a stray blank line silently granting ``ask for permission``
    to the wrong function is the kind of bug nobody finds by reading output.
    """
    out: list[str] = []
    i = index - 1
    while i >= 0:
        body = _strip_comment(lines[i], prefix)
        if body is None:
            break
        out.append(body)
        i -= 1
    out.reverse()
    return out


def _comments_below(lines: list[str], index: int, prefix: str) -> list[str]:
    """Comment bodies opening the body of a definition that starts at *index*.

    Covers the ``def f():`` / ``# ask for permission`` ordering, which reads
    more naturally in Python than putting the marker above the ``def``. Like
    :func:`_comments_above`, the block must abut the definition.
    """
    out: list[str] = []
    i = index + 1
    while i < len(lines):
        body = _strip_comment(lines[i], prefix)
        if body is None:
            break
        out.append(body)
        i += 1
    return out


def _split_params(raw: str) -> tuple[str, ...]:
    """Parameter names from a parameter list, defaults and types dropped."""
    params: list[str] = []
    depth = 0
    current = ""
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            params.append(current)
            current = ""
            continue
        current += ch
    params.append(current)

    out: list[str] = []
    for part in params:
        name = part.split("=")[0].split(":")[0].strip().lstrip("*")
        name = name.lstrip(".")  # JS rest parameters (...args)
        if name and name not in ("self", "cls"):
            out.append(name)
    return tuple(out)


# ── Python ───────────────────────────────────────────────────────────────────

# Module-level ``def``/``async def`` only: an indented definition is a nested
# helper and must not become a callable entry point.
_PY_DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", re.MULTILINE)

_PY_DOCSTRING_RE = re.compile(r'^\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', re.DOTALL)


def _parse_python(source: str) -> list[ParsedFunction]:
    lines = source.splitlines()
    out: list[ParsedFunction] = []
    for idx, line in enumerate(lines):
        if line[:1].isspace():
            continue  # indented → nested, not an entry point
        match = _PY_DEF_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name.startswith("_"):
            continue  # private helper by convention
        markers = _collect_markers(
            _comments_above(lines, idx, "#") + _comments_below(lines, idx, "#")
        )
        description = " ".join(markers.description_lines).strip()
        if not description:
            description = _python_docstring(lines, idx)
        out.append(ParsedFunction(
            name=name,
            ask_for_permission=markers.ask_for_permission,
            allowed_domains=tuple(markers.allowed_domains),
            description=description,
            params=_split_params(match.group(2)),
            line=idx + 1,
        ))
    return out


def _python_docstring(lines: list[str], def_index: int) -> str:
    """First line of the docstring opening the function body, if any."""
    body = "\n".join(lines[def_index + 1: def_index + 12])
    match = _PY_DOCSTRING_RE.match(body)
    if not match:
        return ""
    return match.group(1).strip().splitlines()[0].strip() if match.group(1).strip() else ""


# ── JavaScript ───────────────────────────────────────────────────────────────

_JS_FUNCTION_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"
)
_JS_ARROW_RE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:async\s+)?(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*=>"
)


def _parse_javascript(source: str) -> list[ParsedFunction]:
    lines = source.splitlines()
    out: list[ParsedFunction] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        if line[:1].isspace():
            continue
        match = _JS_FUNCTION_RE.match(line)
        params_raw: str
        if match:
            name, params_raw = match.group(1), match.group(2)
        else:
            arrow = _JS_ARROW_RE.match(line)
            if not arrow:
                continue
            name = arrow.group(1)
            params_raw = arrow.group(2) if arrow.group(2) is not None else (arrow.group(3) or "")
        if name.startswith("_") or name in seen:
            continue
        seen.add(name)
        markers = _collect_markers(
            _comments_above(lines, idx, "//") + _comments_below(lines, idx, "//")
        )
        out.append(ParsedFunction(
            name=name,
            ask_for_permission=markers.ask_for_permission,
            allowed_domains=tuple(markers.allowed_domains),
            description=" ".join(markers.description_lines).strip(),
            params=_split_params(params_raw),
            line=idx + 1,
        ))
    return out


# ── public entry point ───────────────────────────────────────────────────────


def detect_language(file_name: str) -> str:
    """Map a file name to one of :data:`LANGUAGES`. Raises on anything else."""
    for ext, lang in EXTENSION_LANGUAGE.items():
        if file_name.lower().endswith(ext):
            return lang
    raise SkillCodeError(
        f"Unsupported skill file type {file_name!r} — expected one of "
        f"{', '.join(EXTENSION_LANGUAGE)}"
    )


def parse_skill_code(source: str, language: str) -> list[ParsedFunction]:
    """Return the callable functions declared in *source*.

    Raises :class:`SkillCodeError` when the file is too large, declares no
    callable function, or declares more than :data:`MAX_FUNCTIONS`.
    """
    if language not in LANGUAGES:
        raise SkillCodeError(f"Unknown skill language {language!r}")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise SkillCodeError(
            f"Skill file exceeds {MAX_SOURCE_BYTES // 1024} KB"
        )

    functions = (
        _parse_python(source) if language == "python" else _parse_javascript(source)
    )
    if not functions:
        raise SkillCodeError(
            "No callable function found. Declare at least one top-level "
            + ("`def name(...)`" if language == "python" else "`function name(...)`")
            + " whose name does not start with '_'."
        )
    if len(functions) > MAX_FUNCTIONS:
        raise SkillCodeError(
            f"Skill declares {len(functions)} functions — the limit is {MAX_FUNCTIONS}"
        )
    return functions

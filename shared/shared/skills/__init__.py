"""Skill plugin layer: parsing skill code files and the calls that invoke them.

Two independent parsers live here, both pure and side-effect free so they can
be unit-tested without a database or a container runtime:

``code_parser``
    Reads a skill's ``.py``/``.js`` file at import time and derives its
    function table — names, parameters, the ``ask for permission`` flag and
    the per-function network allowlist.

``call_parser``
    Reads a finished LLM answer and finds ``skillname.funktion(args)`` calls,
    returning spans so results can be substituted back into the answer.

Execution itself is not here: it belongs to the evaluation service, which owns
the permission gate, and to the sandbox service, which owns the containers.
"""

from shared.skills.call_parser import (
    MAX_ARGS,
    MAX_CALLS_PER_ANSWER,
    FunctionCall,
    find_calls,
    substitute,
)
from shared.skills.code_parser import (
    LANGUAGES,
    ParsedFunction,
    SkillCodeError,
    detect_language,
    parse_skill_code,
)

__all__ = [
    "FunctionCall",
    "LANGUAGES",
    "MAX_ARGS",
    "MAX_CALLS_PER_ANSWER",
    "ParsedFunction",
    "SkillCodeError",
    "detect_language",
    "find_calls",
    "parse_skill_code",
    "substitute",
]

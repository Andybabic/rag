"""Sub-Agent role configurations.

Each role parameterises the existing ``run_agent`` ReAct loop with a
role-specific system-prompt suffix, action subset and step budget. The
manager picks roles per sub-task; identical roles can run in parallel
with different sub-queries.

Roles are document-focused (no modality cut): they differ in *how* they
search and reason, not in *which* backend they call.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoleConfig:
    """Per-role parameters for a sub-agent run."""

    name: str
    """Stable identifier — used by the manager to address the role and by the
    frontend to label the trace group."""

    label: str
    """Short human-readable label for the UI."""

    prompt_suffix: str
    """Appended to the use-case system prompt for sub-agents in this role.
    Tells the LLM *how* this specialist should approach the sub-query."""

    actions: tuple[str, ...]
    """Allowed action names. Subset of the use-case's enabled actions —
    actions not in this tuple are filtered out before the loop starts."""

    max_steps: int
    """Step budget for this role. Tight budgets keep parallel fan-out fast."""


_FACTS_SUFFIX = """
DEINE ROLLE: Fakten-Spezialist.

Du beantwortest eine eng umrissene Teilfrage und lieferst nur belegte,
konkrete Fakten zurück. Keine Hintergrund-Erklärungen, keine Prozeduren.

- Formuliere die SEARCH-Query so präzise wie möglich (enge Begriffe,
  Eigennamen, Zahlen, Codes).
- Maximal eine Folge-Suche, wenn die erste leer war.
- Antworte knapp: pro Fakt eine Zeile mit Quellenverweis [n].
- Wenn der gesuchte Fakt nicht in den Chunks steht, sage das wörtlich –
  keine Vermutungen.
"""

_PROCEDURE_SUFFIX = """
DEINE ROLLE: Prozedur-Spezialist.

Du beantwortest eine Teilfrage, die nach einem Ablauf, einer Anleitung,
einem Workflow oder einer Reihenfolge fragt.

- Suche gezielt nach Anleitungen/Schritten ("Vorgehen", "Schritt",
  "Ablauf", "Prozedur", "Wartung", "Reinigung", o.ä. je nach Domäne).
- Sammle zusammenhängende Chunk-Sequenzen – bevorzuge Chunks aus
  derselben Quelle/Sektion (Breadcrumbs beachten).
- Antworte als nummerierte Schrittliste. Jeder Schritt mit
  Quellenverweis [n]. Bei Lücken im Ablauf das explizit kennzeichnen.
"""

_CONTEXT_SUFFIX = """
DEINE ROLLE: Kontext-Spezialist.

Du lieferst Hintergrund, Definitionen, Begriffsklärungen und
Rahmenbedingungen zu einer Teilfrage – kein konkretes Faktenwissen,
keine Schritt-für-Schritt-Anleitungen.

- Breitere SEARCH-Query erlaubt (Synonyme, Oberbegriffe).
- Antworte in 2-4 Sätzen, jeder mit Quellenverweis [n].
- Wenn die Chunks nur Fakten oder Prozeduren enthalten und keinen
  Kontext, sage das wörtlich.
"""


ROLE_CONFIGS: dict[str, RoleConfig] = {
    "facts": RoleConfig(
        name="facts",
        label="Fakten",
        prompt_suffix=_FACTS_SUFFIX,
        actions=("SEARCH", "REFINE_QUERY", "FINAL_ANSWER"),
        max_steps=3,
    ),
    "procedure": RoleConfig(
        name="procedure",
        label="Prozedur",
        prompt_suffix=_PROCEDURE_SUFFIX,
        actions=("SEARCH", "REFINE_QUERY", "FINAL_ANSWER"),
        max_steps=3,
    ),
    "context": RoleConfig(
        name="context",
        label="Kontext",
        prompt_suffix=_CONTEXT_SUFFIX,
        actions=("SEARCH", "REFINE_QUERY", "FINAL_ANSWER"),
        max_steps=3,
    ),
}


DEFAULT_ROLE = "facts"
"""Used when the manager cannot decide or returns an unknown role."""


VALID_ROLES: frozenset[str] = frozenset(ROLE_CONFIGS.keys())


def get_role(name: str) -> RoleConfig:
    """Look up a role config, falling back to the default if unknown."""
    return ROLE_CONFIGS.get(name, ROLE_CONFIGS[DEFAULT_ROLE])


def filter_actions(role: RoleConfig, available_actions: list[str]) -> list[str]:
    """Intersect role-allowed actions with what the use case has enabled.

    Use-case-specific retrieval variants (any enabled ``SEARCH*`` action,
    e.g. ``SEARCH_CNC``) augment every role that can already ``SEARCH``.
    Without this, the role allowlist silently strips a specialised retrieval
    tool the use case enabled, so the manager path can never reach it.

    Guarantees FINAL_ANSWER stays in the list — otherwise the loop can never
    terminate properly.
    """
    enabled = set(available_actions)
    filtered = [a for a in role.actions if a in enabled]
    if "SEARCH" in filtered:
        for a in available_actions:
            if a.startswith("SEARCH") and a not in filtered:
                filtered.append(a)
    if "FINAL_ANSWER" not in filtered:
        filtered.append("FINAL_ANSWER")
    return filtered

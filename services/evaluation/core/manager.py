"""Manager-Agent: decompose → fan-out → synthesize.

The manager is the agentic entry point. It:

1. Asks an LLM to break the user query into 1-3 sub-tasks, each with a
   role (facts / procedure / context) and a focused sub-query.
2. Spawns one ``run_agent`` ReAct loop per sub-task in parallel.
3. Calls a synthesizer LLM to merge sub-answers into one final answer
   with consolidated citations.

Sub-agents stay independent: each has its own thought-trace, its own
memory of observations, its own step budget. The manager only sees
their final answers + chunks — not their intermediate reasoning. The
trace is reported via ``on_event`` so the UI can render the hierarchy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from config import settings
from core.agent import _NO_INFO_RE
from core.grounding import find_ungrounded
from core.guards import strip_invented_years
from core.reranker import rerank_chunks
from core.actions import product_scope
from core.agent import run_agent
from core.citations import map_citations, strip_unresolved_refs
from core.llm import (
    TOKENS_COMPLIANCE,
    TOKENS_PLAN,
    TOKENS_SYNTHESIS,
    call_llm,
)
from core.memory_rag import format_memory, recall, remember
from core.skill_functions import apply_skill_functions
from shared.skills import find_calls
from shared.llm import vision_describe
from shared.llm.pipeline import get_last_llm_timing, reset_llm_timing
from shared.usecase_config import list_skill_functions, resolve_config
from core.roles import (
    DEFAULT_ROLE,
    VALID_ROLES,
    RoleConfig,
    filter_actions,
    get_role,
)
from core.timing import subagent_phase_children, timing_report

logger = logging.getLogger(__name__)

StepCallback = Optional[Callable[[dict], Awaitable[None]]]

_MAX_SUBTASKS = 3

# Citation markers inside a sub-agent fragment. Local to that sub-agent's own
# observation list, so meaningless — and misleading — to the synthesizer.
_LOCAL_REF_RE = re.compile(r"\s*\[\d+]")

# A bibliography line: "[3] datei.pdf | 4.18. Wendeanlage". Recognised by the
# marker opening the line AND the rest naming a source rather than stating
# something — a file name, a page, or the breadcrumb separator. Position alone
# is not enough: a body line may legitimately open with its own marker.
_LEADING_REF_RE = re.compile(r"^\s*\[\d+]\s*(.+)$")
_SOURCE_HINT_RE = re.compile(
    r"\.(?:pdf|docx?|txt|md|html?|pptx?|xlsx?)\b|\||S\.\s*\d+", re.IGNORECASE
)


def _is_source_list_line(line: str) -> bool:
    match = _LEADING_REF_RE.match(line)
    return bool(match) and bool(_SOURCE_HINT_RE.search(match.group(1)))


def _body_without_source_list(answer: str) -> str:
    """Drop a trailing block of bibliography lines from *answer*.

    Used to tell a genuine inline citation from a source list appended at the
    end. Both contain ``[n]``, so a plain presence check reads a bibliography
    as full citation coverage — which is how a completely uncited answer
    passed compliance with eight "citations".
    """
    lines = answer.rstrip().split("\n")
    while lines and (not lines[-1].strip() or _is_source_list_line(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def has_inline_citations(answer: str) -> bool:
    """True when at least one ``[n]`` sits next to an actual statement."""
    return bool(re.search(r"\[\d+]", _body_without_source_list(answer)))

# How much of the use-case system prompt to hand the decomposer. Roomy enough
# that a domain glossary appended to the prompt survives: the sub-queries are
# what hit the index, so a term mapping silently cut off here would take the
# retrieval down with it. Prompt processing is not this pipeline's bottleneck,
# so the extra characters are cheap insurance.
_DOMAIN_BRIEF_CHARS = 4000
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)


# ── Decomposer ──────────────────────────────────────────────────────────────

_DECOMPOSER_SYSTEM = """Du bist der Manager-Agent eines dokumenten-fokussierten RAG-Systems.

Deine einzige Aufgabe: die Nutzer-Anfrage in 1-3 sinnvolle Sub-Tasks
zerlegen, die parallel von Spezialisten bearbeitet werden.

Verfügbare Rollen:
- "facts":     konkrete Werte, Zahlen, Namen, Eigenschaften, kurze Fakten
- "procedure": Abläufe, Anleitungen, Schritt-für-Schritt, Workflows
- "context":   Hintergrund, Definitionen, Begriffsklärungen, Rahmen

Antwortform (answer_form) – was die Frage als Antwort erwartet:
- "single":      EIN Wert, Name, Ereignis oder eine Zahl ("Welches Ereignis …?",
                 "Wie viele …?", "Wer …?")
- "list":        eine Aufzählung ("Welche Spieler …?", "Worauf ist zu achten?")
- "comparison":  zwei Seiten gegeneinander ("Was war mehr/früher …?")
- "explanation": ein Ablauf oder Zusammenhang ("Wie funktioniert …?")

Merge-Strategien:
- "complementary": Antworten ergänzen sich (Standard, z.B. Fakt + Hintergrund)
- "comparative":   Antworten werden gegenübergestellt (z.B. A vs. B)
- "fallback":      die Sub-Tasks beantworten dieselbe Frage auf zwei Wegen

REGELN:
- Bei einer eng umrissenen Einzelfrage (ein Wert, ein Name, eine Zahl)
  reicht EIN Sub-Task mit "facts".
- Aufzählende Fragen ("Worauf ist zu achten?", "Was ist zu beachten?",
  "Welche Angaben/Punkte/Schritte ...?") brauchen 2-3 Sub-Tasks, die
  VERSCHIEDENE Facetten abdecken (z.B. Ablauf, Sicherheit/Risiken,
  Sonderfälle/Störungen). Ein einzelner Sub-Task findet dafür zu wenig
  Material: die gesamte Suchbreite ist Anzahl Sub-Tasks × Schrittbudget,
  und aus dem gefundenen Material speist sich die ganze Antwort.
- Bei Vergleichen pro Entität ein eigener Sub-Task (meist "facts").
- "procedure" nur, wenn explizit nach Ablauf/Anleitung gefragt ist.
- "context" nur, wenn Hintergrund/Definitionen wirklich helfen würden.
- NIE mehr als 3 Sub-Tasks. Lieber einen weglassen als zwei ähnliche.
- Jede sub_query muss eigenständig durchsuchbar sein (keine Pronomen,
  keine Bezüge auf andere Sub-Tasks).
- Übernimm Eigennamen, Ereignisbezeichnungen und Zeitangaben der Anfrage
  WÖRTLICH. Ergänze KEINE Jahreszahlen, Orte, Personen oder Deutungen aus
  eigenem Wissen – auch nicht in Klammern. Was die Anfrage offen lässt
  („kürzlich“, „der Konflikt“, „der verfluchte Dienstag“), bleibt offen: Die
  Dokumente klären es, nicht du. Eine falsch ergänzte Jahreszahl lenkt die
  Suche auf ein anderes Ereignis, und der Spezialist verwirft dann den
  richtigen Treffer, weil er nicht zu deiner Jahreszahl passt.
- KEINE Platzhalter in der sub_query. Schreibe nie "(Standort)", "[Name]"
  oder "<Ort>". Setze den konkreten Wert aus der Anfrage ein; fehlt er,
  nimm den Ort/Gegenstand aus dem fachlichen Kontext des Use Cases. Eine
  sub_query mit Platzhalter ist unbeantwortbar — weder die Suche noch ein
  Werkzeug weiss damit etwas anzufangen, und der Spezialist verbrennt seine
  Schritte an einer Frage, die niemand stellen wollte.
- KEIN Sub-Task für reines Allgemeinwissen ("Welche Kleidung trägt man bei
  Regen?", "Was ist ein Fahrplan?"). Dafür gibt es weder Dokumente noch
  Werkzeuge; ein solcher Sub-Task kostet eine volle Agenten-Runde und
  liefert garantiert "keine Information". Lieber ein Sub-Task weniger.

Antworte AUSSCHLIESSLICH mit gültigem JSON in genau diesem Schema:

{
  "rationale": "kurz auf Deutsch, warum du so zerlegst",
  "merge_strategy": "complementary" | "comparative" | "fallback",
  "answer_form": "single" | "list" | "comparison" | "explanation",
  "subtasks": [
    {"role": "facts" | "procedure" | "context",
     "sub_query": "...",
     "focus": "kurze Begründung, was dieser Sub-Task beitragen soll"}
  ]
}

Keine Markdown-Codefences, kein Vorspann, nur das JSON-Objekt.
"""


def _extract_json(text: str) -> dict | None:
    """Extract the first balanced JSON object from a possibly noisy LLM reply."""
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Try to repair common issues — trailing prose after the object, etc.
        depth = 0
        end = -1
        in_str = False
        escape = False
        for i, ch in enumerate(candidate):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            try:
                return json.loads(candidate[:end])
            except json.JSONDecodeError:
                return None
        return None


def _fallback_plan(query: str) -> dict:
    """Default plan when the decomposer fails — single facts sub-agent.

    Keeps the system functional even if the manager LLM returns garbage.
    """
    return {
        "rationale": "Fallback: Manager-LLM lieferte keinen verwertbaren Plan.",
        "merge_strategy": "fallback",
        "answer_form": infer_answer_form(query),
        "subtasks": [
            {
                "role": DEFAULT_ROLE,
                "sub_query": query,
                "focus": "Direkte Beantwortung der Originalfrage.",
            }
        ],
    }


#: Placeholder tokens the decomposer leaves in a sub_query. Instructing it not
#: to produce them helps but does not hold — small models reproduce the shape
#: of the question they were given.
#:
#: Square and angle brackets are always placeholders: nobody writes "[Standort]"
#: meaning a place. Round brackets are not — "(Niederösterreich)", "(Oder)" and
#: "(Bayern)" are exactly how a German question disambiguates a place name, and
#: stripping those would damage a perfectly good sub-query. So round brackets
#: only count when they wrap one of the generic slot words below.
_PLACEHOLDER_WORDS = (
    "standort|ort|ortsname|stadt|adresse|name|nummer|datum|zeitpunkt|zeit|"
    "wert|zahl|betrag|menge|gegenstand|objekt|thema|begriff|platzhalter|"
    "hier einfügen|einfügen|angeben|bitte angeben|xxx|tbd|todo|x|y|z"
)
_PLACEHOLDER_RE = re.compile(
    r"[\[<]\s*[^\]>]{0,40}\s*[\]>]"                       # [irgendwas], <irgendwas>
    # "(Standort)" but also "(Standort: [konkreter Ort])" — the decomposer
    # nests them, and stripping only the inner bracket leaves "(Standort: )".
    rf"|\(\s*(?:{_PLACEHOLDER_WORDS})\s*(?::[^)]*)?\)",
    re.IGNORECASE,
)


def _strip_placeholders(sub_query: str, fallback_query: str) -> str:
    """Remove placeholder tokens from a sub-query, or fall back to the question.

    A sub_query like "Wie wird das Wetter morgen in Gebäude A (Standort)?" is
    unanswerable by construction: the retrieval has nothing to match and a tool
    has nothing to pass along. The specialist then spends its whole step budget
    on a question nobody asked — measured at 30-55 s per occurrence. Left with
    too little to search for, the user's own wording is the better bet.
    """
    if not _PLACEHOLDER_RE.search(sub_query):
        return sub_query
    stripped = _PLACEHOLDER_RE.sub("", sub_query)
    stripped = re.sub(r"\s{2,}", " ", stripped)
    # Removing "(Standort)" from "... für morgen in (Standort)?" leaves a
    # dangling "in ?" — worse than the placeholder, because it reads like a
    # complete question.
    # Removing the placeholders leaves the words that pointed at them:
    # "… morgen in (Standort) und (Standort) sein?" becomes "… morgen in und
    # sein?". Patching that reliably is not worth it — a sub-query with a
    # dangling preposition is a broken question either way, so the caller
    # drops it and the remaining sub-tasks do the work.
    _DANGLING = (
        r"(?:^|\s)(?:in|im|für|fuer|an|am|bei|beim|zu|zum|zur|von|vom|nach|auf)"
        r"(?=\s*(?:und|oder|sowie|bzw\.?)?\s*[?.!:,]*\s*$)"
    )
    stripped = re.sub(_DANGLING, "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+(und|oder|sowie)\s*([?.!:,]*)\s*$", r"\2",
                      stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s{2,}", " ", stripped)
    stripped = re.sub(r"\s+([?.!,;:])", r"\1", stripped).strip(" ,;:").strip()
    # Deliberately NOT discarding a sub-query that still reads a little rough
    # ("… morgen in und sein?"). Tried that: it threw away the weather sub-task
    # of every run, leaving a single sub-task and no tool call at all. A clumsy
    # question the agent can still work with beats no question.
    # Fewer than a handful of real words left means the placeholder *was* the
    # question. Returning the user's original would be a reasonable stand-in
    # for one sub-task — but the decomposer emits several, and two of them
    # collapsing onto the same fallback produced three near-identical sub-tasks
    # that all searched for the same thing. An empty string tells the caller to
    # drop the sub-task instead; one good sub-task beats three copies.
    if len(stripped.split()) < 4:
        return ""
    return stripped


def _with_product_scope(sub_query: str, scoped_product: str | None) -> str:
    """Die Bauteilnummer der Nutzerfrage in jeder sub_query festhalten.

    Die Bauteil-Eingrenzung der Suche liest die Nummer aus der Frage, die der
    Sub-Agent stellt – und die schreibt der Planer neu. Aus „11221.5277, der
    Dm63 Messerkopf fehlt" wird „Ist der Messerkopf Dm63 verfügbar?", die
    Eingrenzung entfällt lautlos, und der Sub-Agent antwortet mit den
    Rüstdaten eines fremden Bauteils.
    """
    if not scoped_product or scoped_product in sub_query:
        return sub_query
    return f"{sub_query} (Bauteil {scoped_product})"


#: What shape the answer must take, by the planner's reading of the question.
#: The synthesizer otherwise answers every question as a list of everything it
#: found – observed: "Welches Ereignis …?" got six numbered criticisms, the
#: one event asked for in fifth place.
_ANSWER_FORMS = {
    "single": (
        "Gefragt ist EIN Wert, Name, Ereignis oder eine Zahl. Nenne ihn im ersten "
        "Satz mit Beleg. Weitere Angaben nur, wenn sie genau diesen einen Wert "
        "belegen oder einschränken – keine Aufzählung weiterer Punkte."
    ),
    "list": (
        "Gefragt ist eine Aufzählung. Liste die Punkte vollständig, je Punkt "
        "ein Satz mit Beleg."
    ),
    "comparison": (
        "Gefragt ist ein Vergleich. Erster Satz: das Fazit (welche Seite, mit "
        "beiden Werten und Belegen). Danach höchstens eine kurze Begründung."
    ),
    "explanation": (
        "Gefragt ist ein Ablauf oder Zusammenhang. Erkläre ihn in der "
        "nötigen Reihenfolge, jeder Schritt belegt."
    ),
}


_COMPARATIVE_RE = re.compile(
    r"\b(?:mehr|weniger|höher\w*|niedriger\w*|größer\w*|kleiner\w*|früher|später|"
    r"zuerst|länger|kürzer|teurer|billiger|besser\w*|schlechter\w*|stärker\w*|"
    r"ehrgeizig\w*|erfolgreich\w*)\b.*\boder\b",
    re.I | re.S,
)
_SINGLE_RE = re.compile(
    r"^\s*(?:wie\s+viele?|wie\s+hoch|wie\s+lange|wann|wo|wer|welche[rs]|was\s+war)\b", re.I
)
_LIST_RE = re.compile(r"^\s*(?:welche|was\s+sind|nenne|worauf|wer\s+sind)\b", re.I)
_EXPLAIN_RE = re.compile(r"^\s*(?:wie\s+funktioniert|warum|wieso|weshalb|erkläre|wie\s+geht)\b", re.I)


def infer_answer_form(query: str) -> str | None:
    """The answer form a question asks for, read off its wording.

    Fallback for when the planner leaves ``answer_form`` out – in practice it
    usually does. Comparison is checked first: "Welches Ereignis forderte mehr
    …: A oder B?" opens like a single-value question but asks to compare.
    """
    q = query or ""
    if _COMPARATIVE_RE.search(q):
        return "comparison"
    if _EXPLAIN_RE.search(q):
        return "explanation"
    if _SINGLE_RE.search(q):
        return "single"
    if _LIST_RE.search(q):
        return "list"
    return None


def _normalise_plan(raw: dict, fallback_query: str) -> dict:
    """Validate + clamp the decomposer output. Always returns a usable plan."""
    if not isinstance(raw, dict):
        return _fallback_plan(fallback_query)

    subtasks_raw = raw.get("subtasks")
    if not isinstance(subtasks_raw, list) or not subtasks_raw:
        return _fallback_plan(fallback_query)

    cleaned: list[dict] = []
    for st in subtasks_raw[:_MAX_SUBTASKS]:
        if not isinstance(st, dict):
            continue
        role = st.get("role") if st.get("role") in VALID_ROLES else DEFAULT_ROLE
        sub_query = (st.get("sub_query") or "").strip()
        if not sub_query:
            continue
        cleaned_query = _strip_placeholders(sub_query, fallback_query)
        if cleaned_query != sub_query:
            logger.info(
                "Removed placeholders from a sub-query: %r → %r",
                sub_query, cleaned_query or "(verworfen)",
            )
            if not cleaned_query:
                continue
            sub_query = cleaned_query
        # Two sub-tasks asking the same thing double the cost and add nothing;
        # stripping placeholders can make previously distinct queries collide.
        if any(c["sub_query"].casefold() == sub_query.casefold() for c in cleaned):
            logger.info("Dropped a duplicate sub-query: %r", sub_query)
            continue
        focus = (st.get("focus") or "").strip()
        cleaned.append({"role": role, "sub_query": sub_query, "focus": focus})

    if not cleaned:
        return _fallback_plan(fallback_query)

    # A plan made up purely of "context" sub-tasks cannot answer the question.
    # The context role is defined as *supplementary* — its prompt forbids it
    # from returning concrete facts ("kein konkretes Faktenwissen") — so such a
    # plan retrieves background only and the synthesizer has no substance to
    # cite. The decomposer produces this occasionally because its own rules
    # allow a single sub-task. Restore a concrete-content contributor instead
    # of letting the whole request degrade to ungrounded prose.
    if all(st["role"] == "context" for st in cleaned):
        logger.info(
            "Plan contained only context sub-tasks — adding a facts sub-task "
            "so the answer has a concrete-content contributor."
        )
        cleaned.insert(0, {
            "role": DEFAULT_ROLE,
            "sub_query": fallback_query,
            "focus": "Konkrete, belegbare Angaben zur Originalfrage.",
        })
        del cleaned[_MAX_SUBTASKS:]

    merge = raw.get("merge_strategy")
    if merge not in ("complementary", "comparative", "fallback"):
        merge = "complementary"
    form = raw.get("answer_form")
    if form not in _ANSWER_FORMS:
        form = infer_answer_form(fallback_query)

    return {
        "rationale": (raw.get("rationale") or "").strip()[:500],
        "merge_strategy": merge,
        "answer_form": form,
        "subtasks": cleaned,
    }


async def describe_uploaded_images(
    images: list[str] | None, *, use_case: str
) -> str:
    """Describe user-uploaded chat images via the vision model.

    Returns a combined text description (one line per image) so the answer can
    be grounded in the image even when the chat model isn't multimodal, and so
    the RAG search can optionally use the image content. Best-effort: a failed
    or empty description for one image is skipped, never raised.
    """
    if not images:
        return ""
    cfg = await resolve_config(use_case)
    model = cfg.vision_model
    if not model:
        logger.warning("No vision model configured — skipping image description")
        return ""

    prompt = (
        "Beschreibe den Inhalt dieses Bildes sachlich, präzise und auf Deutsch. "
        "Gib Text, Zahlen, Tabellen, Diagramme und erkennbare Objekte wieder, "
        "damit die Beschreibung eine spätere Frage dazu beantworten kann."
    )
    descriptions: list[str] = []
    for i, img in enumerate(images, 1):
        # Frontend sends data URIs ("data:image/png;base64,AAAA…"); the provider
        # wants raw base64 without whitespace.
        b64 = img.split(",", 1)[1] if "," in img else img
        b64 = "".join(b64.split())
        if not b64:
            continue
        try:
            desc = await vision_describe(prompt, b64, model=model, config=cfg)
        except Exception as exc:  # noqa: BLE001 — best-effort, never break the query
            logger.warning("Vision description failed for uploaded image %d: %s", i, exc)
            continue
        desc = (desc or "").strip()
        if desc:
            label = f"Bild {i}: " if len(images) > 1 else ""
            descriptions.append(f"{label}{desc}")
    return "\n".join(descriptions)


def _is_vague_query(query: str, history: list[dict] | None) -> bool:
    """Heuristic: is the query too short/underspecified to plan well on its own?

    Cheap gate (no LLM) so memory is only pulled in when it actually helps —
    short queries, or short follow-ups that lean on prior context. Detailed
    queries plan fine without the extra context.
    """
    words = len(query.split())
    if words <= settings.VAGUE_QUERY_MAX_WORDS:
        return True
    # A short-ish follow-up in an ongoing conversation is also often vague.
    if history and words <= settings.VAGUE_QUERY_MAX_WORDS + 3:
        return True
    return False


async def plan_subtasks(
    query: str,
    *,
    use_case: str,
    history: list[dict] | None,
    image_context: str = "",
    memory: str = "",
    domain_prompt: str = "",
    skill_functions: list | None = None,
) -> dict:
    """Ask the manager LLM to decompose the query.

    Returns a normalised plan; falls back to a single ``facts`` sub-task on
    LLM/parse failure. Never raises. When ``memory`` is provided (typically for
    a vague/incomplete query) it is offered to the decomposer so it can turn an
    underspecified request into concrete, well-scoped sub-queries.

    ``domain_prompt`` is the use-case system prompt. Without it the decomposer
    plans blind and resolves domain terms against general knowledge — a query
    about a metro "Ersatzsignal" then yields sub-queries about road junctions,
    which retrieve nothing and force every sub-agent into an extra REFINE_QUERY
    round. The sub-queries are what actually hit the index, so their vocabulary
    has to match the corpus.
    """
    user_msg_parts = [f"Nutzer-Anfrage: {query}"]
    if memory:
        user_msg_parts.append(
            "Bekannter Kontext aus dem Gedächtnis dieses Use Cases (nutze ihn, "
            "um eine schwammige oder unvollständige Anfrage zu präzisieren; "
            "erfinde nichts hinzu, was weder Anfrage noch Gedächtnis hergeben):\n"
            f"{memory}"
        )
    if image_context:
        user_msg_parts.append(
            "Hinweis: Der Nutzer hat ein Bild hochgeladen. Erkannter Bildinhalt:\n"
            f"{image_context}\n"
            "Wenn sich die Frage auf das Bild bezieht, plane keine (oder nur "
            "ergänzende) Dokumenten-Suche."
        )
    if history:
        # Last user message before the current one helps disambiguate
        # short follow-ups like "und beim Modell 2699?".
        prior_user = [m["content"] for m in history if m.get("role") == "user"][-2:]
        if prior_user:
            user_msg_parts.append("Vorherige Nutzer-Nachrichten:")
            user_msg_parts.extend(f"- {m}" for m in prior_user)

    system = _DECOMPOSER_SYSTEM

    # Without this the planner only knows how to search documents, so a part of
    # the question that only a tool can answer is dropped during decomposition
    # — and once a sub-task for it does not exist, no sub-agent can rescue it.
    # Observed: "wird es regnen? welchen Bus…" produced two bus sub-tasks and
    # silently lost the weather half.
    if skill_functions:
        lines = [
            "",
            "ZUSÄTZLICHE WERKZEUGE (kein Dokumentenbestand):",
            "Für diesen Use Case stehen den Spezialisten ausserdem diese "
            "Funktionen zur Verfügung. Sie liefern Live-Daten, die in KEINEM "
            "Dokument stehen können:",
        ]
        by_skill: dict[str, list] = {}
        for fn in skill_functions:
            by_skill.setdefault(fn.skill_name, []).append(fn)
        for skill_name, fns in by_skill.items():
            overview = (fns[0].skill_overview or "").strip()
            lines.append(f"- {skill_name}" + (f" — {overview}" if overview else ""))
            for fn in fns:
                description = f": {fn.description}" if fn.description else ""
                lines.append(f"    {skill_name}.{fn.signature or fn.name}{description}")
        lines += [
            "",
            "REGEL: Wenn ein Teil der Anfrage von einem dieser Werkzeuge "
            "beantwortet werden kann, MUSS dieser Teil einen EIGENEN Sub-Task "
            "bekommen — auch dann, wenn die Dokumente dazu nichts hergeben. "
            "Genau dafür sind die Werkzeuge da.",
            "Lass NIE einen Teil der Anfrage weg, nur weil er nicht nach "
            "Dokumenten klingt. Eine Frage nach Wetter, Messwerten oder "
            "aktuellen Zuständen ist ein vollwertiger Sub-Task.",
            "Formuliere solche sub_queries als die Frage selbst (z.B. "
            "„Wie wird das Wetter morgen in St. Pölten?“), nicht als "
            "Suchbegriffe — der Spezialist ruft damit das Werkzeug auf.",
        ]
        system += "\n".join(lines) + "\n"

    if domain_prompt.strip():
        if len(domain_prompt.strip()) > _DOMAIN_BRIEF_CHARS:
            # Silence here would look like the planner ignoring the domain: a
            # glossary usually sits at the end of a system prompt, so that is
            # exactly the part a quiet cut removes.
            logger.warning(
                "Use-case prompt for %s is %d chars — the decomposer only sees "
                "the first %d. Move domain terms/glossary to the top.",
                use_case, len(domain_prompt.strip()), _DOMAIN_BRIEF_CHARS,
            )
        system += (
            "\nFACHLICHER KONTEXT DES USE CASES (nur zur Einordnung der "
            "Begriffe — er beschreibt, worum es in den durchsuchten Dokumenten "
            "geht):\n"
            f"{domain_prompt.strip()[:_DOMAIN_BRIEF_CHARS]}\n\n"
            "Deute Fachbegriffe der Anfrage IMMER in diesem Kontext. Die "
            "FACHBEGRIFFE der Anfrage (Eigennamen, Anlagen, Signale, "
            "Vorschriften) müssen unverändert in den sub_queries vorkommen — "
            "ersetze sie nicht durch Begriffe aus einer anderen Domäne und "
            "erfinde keine Synonyme, die im Dokumentbestand nicht vorkommen.\n"
            "Das gilt für die Begriffe, NICHT für den Fragesatz: eine "
            "sub_query, die bloss die Nutzerfrage wiederholt, ist keine "
            "Zerlegung. Jede sub_query deckt eine eigene Facette ab und "
            "formuliert sie mit den Fachbegriffen dieser Domäne aus."
        )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_msg_parts)},
    ]

    try:
        raw_response = await call_llm(
            messages, use_case=use_case, max_tokens=TOKENS_PLAN
        )
    except Exception as exc:
        logger.warning(f"Manager decomposer LLM failed: {exc}")
        return _fallback_plan(query)

    parsed = _extract_json(raw_response)
    if parsed is None:
        logger.info(
            "Manager decomposer returned non-JSON; using fallback plan. "
            f"Response (truncated): {raw_response[:200]!r}"
        )
        return _fallback_plan(query)
    plan = _strip_invented_years(
        _normalise_plan(parsed, query),
        "\n".join(user_msg_parts) + "\n" + domain_prompt,
    )
    return _keep_single_question(plan, query, history)


def _keep_single_question(plan: dict, query: str, history: list[dict] | None) -> dict:
    """One facet: search with the user's own question, not the planner's rewrite.

    With a single sub-task there is nothing to split, so a rewrite can only
    lose or add – observed: "trotz der Leistung von Kane" became "obwohl Kane
    als bester Spieler des Turniers galt", a premise the documents contradict,
    and the answer repeated it. Kept for follow-ups and vague questions, where
    the planner's rewrite carries context the bare question lacks.
    """
    subtasks = plan.get("subtasks") or []
    if len(subtasks) == 1 and not history and not _is_vague_query(query, history):
        subtasks[0]["sub_query"] = query
    return plan


def _strip_invented_years(plan: dict, allowed_text: str) -> dict:
    """Remove years from sub-queries that appear nowhere in what the planner
    was given (query, history, memory, domain prompt). See ``core.guards``.

    Measured on 100 benchmark questions: "der verfluchte Dienstag" became
    "Beirut 2020" (it was 2024), "der Militäreinmarsch" became "1982".
    """
    for st in plan.get("subtasks", []):
        original = st["sub_query"]
        cleaned = strip_invented_years(original, allowed_text)
        if cleaned != original:
            logger.info("Removed invented year(s) from sub-query: %r → %r", original, cleaned)
            st["sub_query"] = cleaned
    return plan


# ── Sub-agent orchestration ─────────────────────────────────────────────────


def _build_sub_system_prompt(
    base_prompt: str, role: RoleConfig, focus: str, user_query: str = ""
) -> str:
    """Compose the use-case prompt with the role-specific suffix + focus hint.

    ``user_query`` is the question as the user asked it. A specialist used to
    see only its sub-query, so whatever the planner got wrong there (an
    invented year, a misread event) was invisible to it – it could only
    answer the distorted question.
    """
    focus_block = f"\nFOKUS DIESES SUB-TASKS: {focus}\n" if focus else ""
    query_block = (
        "\nGESAMTFRAGE DES NUTZERS (zur Einordnung – dein Sub-Task deckt einen "
        f"Teil davon ab): {user_query}\n"
        "Weicht deine Teilfrage in Ereignis, Ort oder Zeitraum von der "
        "Gesamtfrage ab, gilt die Gesamtfrage.\n"
        if user_query else ""
    )
    return f"{base_prompt}\n{role.prompt_suffix}{focus_block}{query_block}"


#: Events that must reach the client unwrapped. A sub-agent event is normally
#: a trace entry the UI groups under its agent; these four drive a dialog the
#: user has to answer and a live status line, and the frontend matches them on
#: the top-level ``type``.
_PASSTHROUGH_EVENTS = frozenset({
    "permission_request", "permission_resolved",
    "function_started", "function_result",
})


def _wrap_event(
    on_event: StepCallback, *, sub_id: str, role: str, deadline=None
) -> StepCallback:
    """Wrap an ``on_event`` callback so sub-agent events are tagged.

    Returns ``None`` if the caller didn't supply a callback — keeps the
    ``run_agent`` signature happy.

    *deadline* is the sub-agent's :func:`asyncio.timeout` context. When a
    permission prompt goes out, the agent stops working and a human starts
    thinking — and that time must not count against a budget meant for the
    agent's own work. Without pushing the deadline out, a user who takes 60 s
    to decide leaves the specialist 90 s to finish, and one who takes longer
    than the budget kills it outright.
    """
    if on_event is None:
        return None

    async def tagged(event: dict) -> None:
        # Re-emit ReAct events under a sub-agent-scoped envelope so the
        # frontend can group them. We don't forward 'started' / 'final' from
        # the inner loop — the manager emits its own lifecycle events.
        inner_type = event.get("type")
        if inner_type in ("started", "final"):
            return
        if inner_type == "permission_request" and deadline is not None:
            try:
                deadline.reschedule(
                    asyncio.get_running_loop().time()
                    + settings.SUBAGENT_TIMEOUT_S
                    + float(event.get("timeout_s") or 0)
                )
            except (RuntimeError, AttributeError) as exc:  # already expired
                logger.debug("Could not extend the sub-agent deadline: %s", exc)

        if inner_type in _PASSTHROUGH_EVENTS:
            # These are not trace entries, they are a conversation with the
            # user. Wrapping a permission_request made it invisible to the
            # frontend, which matches on the top-level type — so the dialog
            # never appeared, the request timed out as "denied" after 120 s,
            # and the sub-agent then burned its whole 150 s budget waiting.
            await on_event({**event, "subagent_id": sub_id,
                            "subagent_role": role})
            return
        await on_event({
            "type": "subagent_step",
            "subagent_id": sub_id,
            "subagent_role": role,
            "inner_type": inner_type,
            "payload": event,
        })

    return tagged


async def _run_one_subagent(
    *,
    sub_id: str,
    sub_query: str,
    user_query: str = "",
    role: RoleConfig,
    focus: str,
    use_case: str,
    session_id: str,
    use_case_prompt: str,
    available_actions: list[str],
    collection: str,
    filters: dict | None,
    images: list[str] | None,
    image_context: str,
    on_event: StepCallback,
    request_id: str | None = None,
    username: str | None = None,
) -> dict:
    """Run a single sub-agent. Wraps ``run_agent`` with the role config."""
    _t0 = time.perf_counter()
    sub_actions = filter_actions(role, available_actions)
    sub_prompt = _build_sub_system_prompt(use_case_prompt, role, focus, user_query)

    if on_event:
        await on_event({
            "type": "subagent_started",
            "subagent_id": sub_id,
            "subagent_role": role.name,
            "role_label": role.label,
            "sub_query": sub_query,
            "focus": focus,
        })

    try:
        # Deadline per sub-agent: the fan-out below is a gather, so one agent
        # stuck on an unresponsive LLM call would otherwise define the whole
        # phase duration. Cutting it loose lets the healthy specialists' output
        # reach the synthesizer instead of the request stalling on the slowest.
        async with asyncio.timeout(settings.SUBAGENT_TIMEOUT_S) as _deadline:
            result = await run_agent(
                query=sub_query,
                use_case=use_case,
                session_id=session_id,
                system_prompt=sub_prompt,
                available_actions=sub_actions,
                collection=collection,
                filters=filters,
                max_steps=role.max_steps,
                history=None,  # sub-agents work on the focused sub-query directly
                images=images,
                image_context=image_context,
                persist_memory=False,  # the manager owns the moderated memory update
                anchor_query=user_query,
                request_id=request_id,
                username=username,
                on_event=_wrap_event(
                    on_event, sub_id=sub_id, role=role.name, deadline=_deadline
                ),
            )
    except asyncio.TimeoutError:
        logger.warning(
            f"Sub-agent {sub_id} ({role.name}) exceeded "
            f"{settings.SUBAGENT_TIMEOUT_S}s — dropped from the fan-out"
        )
        result = {
            "answer": "",
            "citations": [],
            "agent_steps": [],
            "chunks": [],
            "sufficient": False,
            "error": (
                f"Zeitlimit von {settings.SUBAGENT_TIMEOUT_S:.0f}s überschritten – "
                "dieser Spezialist hat nichts beigetragen."
            ),
        }
    except Exception as exc:
        logger.exception(f"Sub-agent {sub_id} ({role.name}) crashed: {exc}")
        result = {
            "answer": "",
            "citations": [],
            "agent_steps": [],
            "chunks": [],
            "sufficient": False,
            "error": str(exc),
        }

    chunks = []
    for step in result.get("agent_steps", []):
        for c in step.get("chunks", []) or []:
            chunks.append(c)

    sub_result = {
        "subagent_id": sub_id,
        "role": role.name,
        "role_label": role.label,
        "sub_query": sub_query,
        "focus": focus,
        "answer": result.get("answer", ""),
        "agent_steps": result.get("agent_steps", []),
        "chunks": chunks,
        "sufficient": result.get("sufficient", False),
        "searched_collections": result.get("searched_collections", []),
        "error": result.get("error"),
        "duration_ms": round((time.perf_counter() - _t0) * 1000),
    }

    if on_event:
        await on_event({
            "type": "subagent_done",
            "subagent_id": sub_id,
            "subagent_role": role.name,
            "answer_preview": sub_result["answer"][:200],
            "chunk_count": len(chunks),
            "sufficient": sub_result["sufficient"],
            "error": sub_result["error"],
            "duration_ms": sub_result.get("duration_ms"),
        })
    return sub_result


# ── Synthesizer ─────────────────────────────────────────────────────────────


_COMPLIANCE_SYSTEM = """Du bist der Compliance-Agent. Du prüfst die finale
Antwort eines dokumenten-fokussierten RAG-Systems gegen den globalen
Chunk-Pool. Du veränderst die Antwort NICHT — du gibst nur ein Urteil ab.

PRÜFKRITERIEN:
1. Citation-Coverage: Wird jede Tatsachenbehauptung belegt — mit [n] oder durch
   einen Skill-Funktionsaufruf (siehe unten)?
2. Quellenechtheit: Belegen die referenzierten Chunks die Aussage tatsächlich?
3. Halluzination: Enthält die Antwort Fakten, die weder in einem Chunk stehen
   noch aus einem Skill-Aufruf stammen?
4. Use-Case-Policy: Wenn der System-Prompt Regeln nennt, werden sie eingehalten?

SKILL-FUNKTIONSAUFRUFE (zweite zulässige Quelle):
- Ein Ausdruck der Form skill.funktion("argument") im Antworttext ist KEINE
  Halluzination und KEIN fehlender Beleg. Es ist ein Platzhalter: die Plattform
  führt die Funktion nach deiner Prüfung aus und ersetzt ihn durch das echte
  Ergebnis. Der Wert stammt also nicht vom Modell.
- Solche Aufrufe brauchen KEIN [n]. Sie stehen naturgemäss in keinem Chunk —
  das ist erwartet und darf NIEMALS beanstandet werden.
- Eine Antwort, die nur aus Text plus solchen Aufrufen besteht, ist "OK", auch
  wenn der Chunk-Pool leer ist oder kein einziges [n] vorkommt.
- Ein Satz wie „laut Skill wetter" ist die korrekte Quellenangabe dafür.
- Werte aus einer OBSERVATION eines Skill-Aufrufs sind ebenso belegt. Sie
  stehen in keinem Chunk — das ist erwartet und nie ein Mangel. Erkennbar an
  einer Herkunftsangabe im Satz wie „laut Skill wetter".
- Ein solcher Wert braucht KEIN [n], und ein [n] dahinter ist sogar falsch:
  es verweist auf ein Dokument, das ihn nicht belegt. Beanstande das ruhig.

VORGEHEN (verbindlich):
- Lies den zitierten Chunk VOLLSTÄNDIG, bis zum letzten Zeichen, bevor du
  eine Aussage beanstandest. Der Beleg steht oft im letzten Satz.
- Bevor du etwas als unbelegt oder halluziniert meldest, zitiere in "issues"
  die Stelle des Chunks, an der du den Beleg erwartet hättest. Findest du ihn
  dort nicht, schreibe das ausdrücklich. Eine Beanstandung ohne diesen
  Nachweis ist unzulässig.
- Ein Beleg muss nicht wörtlich übereinstimmen — sinngleiche Formulierung
  genügt. Eine Antwort, die einen Chunk nahezu wörtlich wiedergibt, ist der
  Idealfall und nicht verdächtig.
- Chunks sind Ausschnitte. Sie enden regelmässig mitten im Satz oder Wort,
  und Überschriften-Artefakte des PDF-Layouts ("# wird zu") stehen mitten
  darin. Beides trennt keine Quellen und ist KEIN Grund, den vorhandenen
  Text anzuzweifeln.
- Im Zweifel "OK". Ein REWRITE kostet einen kompletten zweiten Synthese-Lauf,
  bei dem der Synthesizer die Quellennummern neu vergeben muss — ein
  unnötiges REWRITE hat dabei schon eine korrekte Aussage an eine falsche
  Quelle gehängt. Beanstande nur, was du konkret benennen kannst.

URTEIL (genau eines):
- "OK"      → Antwort darf raus.
- "REWRITE" → Synthesizer soll neu formulieren. Du nennst genau, was korrigiert
              werden muss.
- "REFUSE"  → Antwort ist nicht zu retten (massive Halluzination o.ä.).

Antworte AUSSCHLIESSLICH mit gültigem JSON in diesem Schema:

{"verdict": "OK" | "REWRITE" | "REFUSE",
 "issues": ["kurze Liste konkreter Probleme, leer wenn OK"],
 "guidance": "wenn REWRITE: kurzer Hinweis für den Synthesizer, was zu ändern ist"}

Kein Vor- oder Nachspann, nur das JSON.
"""


# Compliance issue keyword classification
_COMPLIANCE_KEYWORDS: dict[str, list[str]] = {
    "citation_coverage": [
        "zitat", "citation", "[n]", "referenz", "verweis",
        "quelle nicht angegeben", "fehlende quellenangabe", "unbelegt",
        "fehlende zitation", "zitierung fehlt", "ohne quellenangabe",
        "quellenbezeichnung", "quellenangabe", "quellenverweis",
        "ziffer", "fehlende nummer", "fehlende ziffer",
    ],
    "source_authenticity": [
        "falsche quelle", "belegkraft", "echtheit",
        "nicht belegbar", "widerspruch", "widerspricht",
        "stuetzt nicht", "beweist nicht",
        "falsch zitiert", "falsch wiedergegeben",
        "inkonsistenz", "widerspruechlich",
    ],
    "hallucination": [
        "halluzin", "erfunden", "frei erfunden", "nicht in chunk",
        "existiert nicht", "kein chunk", "fiktiv",
        "existier", "nicht vorhanden", "gibt es nicht",
        "nicht existier", "nicht-existier",
    ],
    "use_case_policy": [
        "richtlinie", "policy", "vorgabe", "satzlaenge", "format",
        "regel", "irrelevant", "meta-kommentar", "meta-kommentare",
        "grenze", "max.", "antwort-format", "laenge", "ton",
        "umgangssprache", "nicht erlaubt", "nicht zulaessig",
        "verstoss", "anforderung", "soll", "muss", "darf nicht",
        "unklar", "zuordnung", "unverstaendlich", "mapping",
        "thema verfehlt", "passt nicht", "ungeeignet",
    ],
}

_CATEGORY_LABELS: dict[str, str] = {
    "citation_coverage": "Zitierabdeckung",
    "source_authenticity": "Quellenechtheit",
    "hallucination": "Halluzination",
    "use_case_policy": "Use-Case-Richtlinie",
}

_SYNTHESIZER_SYSTEM_TEMPLATE = """Du bist der Synthesizer-Agent.

Du bekommst die Originalfrage des Nutzers, eine Liste von Antwort-
Fragmenten verschiedener Spezialisten und einen globalen Chunk-Pool
mit fortlaufender Nummerierung. Erzeuge die finale Antwort.

MERGE-STRATEGIE: {strategy}
- complementary: die Fragmente ergänzen sich — fasse sie zu einer
  zusammenhängenden Antwort zusammen.
- comparative:   stelle die Fragmente explizit gegenüber (z.B. als
  kurze Tabelle oder klar getrennte Abschnitte).
- fallback:      die Fragmente decken dieselbe Frage ab — wähle die
  belastbarere Antwort und nimm Ergänzungen aus den anderen.

REGELN (hart):
- Verwende AUSSCHLIESSLICH die globalen Chunks aus dem GLOBAL_CHUNK_POOL
  als Faktenquelle. Die Antwort-Fragmente der Spezialisten sind nur
  Vorschläge — prüfe gegen den Pool.
- AUSNAHME: Werte aus einer ausgeführten Skill-Funktion. Erkennbar an einer
  Herkunftsangabe, die BEREITS IM FRAGMENT STEHT, z.B. „laut Skill wetter".
  Schreibe diese Formel NIEMALS selbst hin. Steht sie in keinem Fragment, gab
  es keinen Skill-Aufruf — dann ist jeder Wert, den du ihm zuschreibst, frei
  erfunden. Das ist der schwerste Fehler, den du machen kannst. Sie stammen aus einer
  definierten Quelle, nicht vom Modell, und stehen naturgemäss in KEINEM
  Chunk. Übernimm sie unverändert in die Antwort und behalte die
  Herkunftsangabe bei. Sie brauchen KEIN [n] — ein [n] dahinter wäre sogar
  falsch, weil es auf ein Dokument zeigt, das den Wert nicht belegt.
- Ein solcher Wert ist eine vollwertige Antwort auf seinen Teil der Frage.
  Schreibe NIE „dazu enthalten die Dokumente keine Information", wenn ein
  Fragment den Wert bereits geliefert hat — das verwirft eine korrekte
  Auskunft und ist der schlimmere Fehler.
- Jede Tatsachenbehauptung muss mit [n] belegt sein, wobei n die globale
  Chunk-Nummer ist (nicht die lokale Nummerierung der Spezialisten).
- Der GLOBAL_CHUNK_POOL nennt dir den gültigen Nummernbereich. Zitiere
  AUSSCHLIESSLICH Nummern aus diesem Bereich; eine Nummer ausserhalb wird
  nachträglich entfernt. Das ist KEIN Grund, seltener zu zitieren: eine
  Antwort ganz ohne [n] ist falsch — es sei denn, sie besteht ausschliesslich
  aus Skill-Werten (siehe Ausnahme oben). Im Zweifel nimm die Nummer des
  Chunks, aus dem die Aussage stammt.
- PLATZIERUNG: Das [n] steht DIREKT bei der Aussage, die es belegt — am
  Ende des jeweiligen Satzes oder Listenpunkts, mitten im Text. So sieht
  der Leser bei jeder einzelnen Aussage, worauf sie sich stützt.
- Eine gesammelte Quellenliste am Ende der Antwort ist VERBOTEN. Also
  keine Zeilen der Form "[1] datei.pdf | Abschnitt", kein
  Literaturverzeichnis, keine "Quellen:"-Sektion. Die Oberfläche zeigt
  die Fundstellen ohnehin separat an; ein [n], das nur in so einer Liste
  steht und neben keiner Aussage, belegt nichts.

BEISPIEL (Platzierung):
  richtig: "Der Zug wird in der Wendeanlage umgerüstet [4]. Vor der
           Einfahrt ist eine Durchsage zu tätigen [2]."
  falsch:  "Der Zug wird in der Wendeanlage umgerüstet. Vor der Einfahrt
           ist eine Durchsage zu tätigen.\n\n[2] datei.pdf\n[4] datei.pdf"
- Der Pool ist nach Relevanz sortiert: [1] ist der Chunk, der am besten
  zur Frage passt.
- SCHÖPFE DIE TRAGENDEN CHUNKS AUS. Geh die vordersten Chunks durch und
  prüfe für jede einzelne Aussage darin, ob sie zur Frage gehört. Alles,
  was zur Frage gehört, kommt in die Antwort — auch Nebenbedingungen,
  Rückmeldungen des Systems, Einschränkungen und Folgen ("… dadurch
  ändert sich auch …", "… gilt nur, wenn …"). Ein Fragment eines
  Spezialisten ist eine Zusammenfassung; deine Antwort ist es nicht.
  Kürze den tragenden Chunk NICHT auf seinen Kernsatz ein.
- Das ist KEINE Aufforderung zur Länge. Ein Chunk, der eine ANDERE
  Funktion oder ein anderes Gerät beschreibt, gehört nicht in die
  Antwort — auch nicht als zusätzlicher Schritt, auch nicht "der
  Vollständigkeit halber". Übertrage niemals eine Eigenschaft von einem
  Chunk auf den Gegenstand eines anderen (z.B. eine Bestätigungsregel,
  die für Element A gilt, auf Element B). Vollständig heisst: alles zur
  gestellten Frage, nichts darüber hinaus.
- Widersprüche zwischen Quellen NICHT überdecken — markiere sie
  ("Quelle [3] gibt X an, Quelle [5] dagegen Y").
- Wenn ein Teil der Originalfrage durch keinen Chunk gedeckt ist, sage
  das wörtlich.
- Jedes Fragment trägt einen STATUS. „nicht gefunden" oder „teilweise"
  heisst nur, dass der Spezialist nichts Passendes erkannt hat. Nennt das
  Fragment Pool-Stellen „laut Reranker passend", prüfe genau diese Chunks
  und beantworte den Teil daraus, wenn sie ihn tragen. Steht dort „nichts",
  ist der Teil offen – sage das, ohne zu raten.
- Ein Fragment „keine Information" ist kein Beweis: Prüfe den Pool selbst.
  Steht dort eine passende Angabe – auch ein naher Treffer (benachbarter
  Ort, ungefähre Zahl, angrenzender Zeitraum) –, nimm sie auf und benenne
  die Abweichung im selben Satz.
- VERGLEICHSFRAGEN („Was war mehr / früher / teurer …?"): Liegen für BEIDE
  Seiten Werte vor, nenne beide mit [n] und ziehe das Fazit ausdrücklich.
  Fehlt eine Seite, sage welche, nenne die vorhandene und ziehe KEIN Fazit –
  ein Vergleich gegen einen fehlenden Wert ist geraten.
- Ist ein Fragment mit "(Fehler: …)" markiert, hat dieser Spezialist
  nichts geliefert. Antworte dann nur aus den vorhandenen Chunks und
  weise am Ende in einem Satz darauf hin, welcher Aspekt deshalb
  ungeprüft blieb. Diesen Ausfall NICHT stillschweigend übergehen.
- KEINE eigenen Ergänzungen, kein Allgemeinwissen, keine Trainings-Daten.
- Antworte ohne Vor- oder Nachspann, ohne Meta-Kommentare.
- Die interne Arbeitsteilung bleibt unsichtbar: keine Rollennamen
  ("Procedure", "Facts", "Kontext"), keine Erwähnung von Spezialisten,
  Fragmenten oder Chunk-Pool in Überschriften oder Fliesstext. Der Nutzer
  sieht eine Antwort, nicht deren Entstehung.

BILDER:
- Im AVAILABLE_IMAGES-Block stehen Bilder, die zu zitierten Chunks gehören
  (mit ID, Seite und Kurzbeschreibung).
- Wenn ein Bild deine konkrete Aussage stützt (z.B. ein Signal, ein
  Diagramm, eine Tabelle, ein Schaltplan), zitiere es genau einmal an der
  passenden Stelle im Fließtext mit dem Marker [BILD: <image_id>].
- Erfinde NIE eine image_id. Verwende ausschliesslich IDs aus
  AVAILABLE_IMAGES. Wenn kein Bild beiträgt, lasse alle Marker weg.
"""


def _merge_chunks(subagents: list[dict]) -> list[dict]:
    """Build the global chunk pool by deduplicating across sub-agents.

    Dedup key is the first 200 characters of the chunk text (matches the
    dedup heuristic already used in actions.action_search).

    The pool is returned sorted by retrieval score, best first. Its order *is*
    the citation numbering (``map_citations`` resolves ``[n]`` to
    ``chunks[n-1]``), so sorting here — not in the renderer — keeps the numbers
    the synthesizer sees and the sources they resolve to in sync. Sub-agent
    order carries no meaning for the reader, whereas relevance does: it lets
    the prompt name the leading sources without exposing raw scores, whose
    scale differs by an order of magnitude between a live cross-encoder and
    the RRF fallback.
    """
    seen: dict[str, dict] = {}
    order: list[str] = []
    for sub in subagents:
        for chunk in sub.get("chunks", []):
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            key = text[:200]
            existing = seen.get(key)
            if existing is None:
                seen[key] = dict(chunk)
                order.append(key)
            else:
                # Keep the higher-scored copy
                if (chunk.get("score") or 0) > (existing.get("score") or 0):
                    seen[key] = dict(chunk)
    return sorted(
        (seen[k] for k in order),
        key=lambda c: c.get("score") or 0.0,
        reverse=True,
    )


# Per-chunk character budget for the synthesizer's chunk pool.
#
# This pool is the synthesizer's ONLY permitted source of fact, so whatever is
# cut here cannot appear in the answer no matter how the prompt is worded. The
# previous 400 truncated every single retrieved chunk — the chunker emits
# ~500-character chunks, so the cut consistently removed the tail, which is
# where a section's consequence sentence tends to sit ("Ändert das Fahrpersonal
# die Ausstiegsseite, ändert sich ebenfalls das Aussehen der Pfeile"). The
# answer then read as an accurate but oddly incomplete summary of a source the
# synthesizer had never seen whole. Sized to pass a normal chunk intact with
# headroom for oversized ones (tables, appended image descriptions).
#
# The neighbour context shown around a hit (CONTEXT_NEIGHBOR_CHARS before and
# after) comes on top – cutting it here would hide from the synthesizer what
# the specialist based its fragment on.
_POOL_CHUNK_CHARS = 1200 + 2 * max(settings.CONTEXT_NEIGHBOR_CHARS, 0)


def _format_global_chunks(chunks: list[dict]) -> str:
    """Render the global chunk pool for the synthesizer prompt.

    ``chunks`` arrives sorted by relevance (see ``_merge_chunks``), so the
    numbering the synthesizer cites against runs best-first.
    """
    if not chunks:
        return "(Keine Chunks – die Spezialisten haben nichts gefunden.)"
    lines = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("metadata") or {}
        text = (c.get("context_text") or c.get("text") or "")[:_POOL_CHUNK_CHARS]
        file_name = meta.get("file_name", "")
        page = meta.get("page")
        ref = f"{file_name}, S. {page}" if page else file_name
        lines.append(f"[{i}] ({ref}) {text}")
    return "\n".join(lines)


def _collect_available_images(chunks: list[dict]) -> list[dict]:
    """Pull every image reference dropped by the chunker into a flat,
    deduplicated catalog. The synthesizer offers these to the LLM as
    'images you may cite with [BILD: <id>]'.
    """
    seen: dict[str, dict] = {}
    for c in chunks:
        extra = ((c.get("metadata") or {}).get("extra")) or {}
        for img in extra.get("images") or []:
            img_id = img.get("id") or img.get("image_id")
            if not img_id or img_id in seen:
                continue
            seen[img_id] = {
                "id": img_id,
                "page": img.get("page"),
                # Not truncated: for a labelled graphic the description is a
                # transcription of the very values the answer needs (key
                # assignments, table cells). A 300-char cut lands in the middle
                # of that list and silently drops the answer.
                "alt_text": (img.get("alt_text") or "").strip(),
                "url": img.get("url", ""),
            }
    return list(seen.values())


def _format_available_images(images: list[dict]) -> str:
    """Render the image catalog for the synthesizer prompt."""
    if not images:
        return "(Keine Bilder zu diesen Chunks.)"
    lines = []
    for img in images:
        page = img.get("page")
        page_hint = f" S.{page}" if page else ""
        alt = img.get("alt_text") or "(ohne Beschreibung)"
        lines.append(f"- {img['id']}{page_hint}: {alt}")
    return "\n".join(lines)


# Tolerant of markdown the LLM sometimes adds inside the marker, e.g.
# ``[BILD: **img_...**]`` or ``[BILD: `img_...`]`` — otherwise the image is
# never collected into images_used and thus never shown.
_BILD_MARKER_RE = re.compile(r"\[BILD:[^\]]*?(img_[A-Za-z0-9_]+)[^\]]*?\]")


def select_used_images(answer: str, catalog: list[dict]) -> list[dict]:
    """Return the subset of ``catalog`` that the answer actually cites.

    Drops hallucinated IDs silently — the frontend only renders what is
    backed by a real persisted image.
    """
    if not answer or not catalog:
        return []
    by_id = {img["id"]: img for img in catalog}
    used: list[dict] = []
    seen: set[str] = set()
    for match in _BILD_MARKER_RE.finditer(answer):
        img_id = match.group(1)
        if img_id in seen or img_id not in by_id:
            continue
        seen.add(img_id)
        used.append(by_id[img_id])
    return used


# ── Abdeckung: was jede Teilfrage tatsächlich geliefert hat ─────────────────

_FRAGMENT_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_SEARCH_ACTIONS = ("SEARCH", "REFINE_QUERY", "SEARCH_CNC", "SEARCH_TOOL")
_STATUS_LABEL = {
    "found": "gefunden",
    "partial": "teilweise",
    "not_found": "nicht gefunden",
    "error": "Fehler",
}


def facet_status(sub: dict) -> str:
    """found | partial | not_found | error – read off the fragment itself.

    A fragment is prose, and its verdict used to travel to the synthesizer as
    prose too: "Dazu enthalten die Dokumente keine Information" was final.
    Made explicit here, the verdict becomes something the manager can check
    against the pool instead of trusting.
    """
    if sub.get("error"):
        return "error"
    sentences = [x for x in _FRAGMENT_SENTENCE_RE.split((sub.get("answer") or "").strip()) if x.strip()]
    if not sentences:
        return "not_found"
    open_sentences = [x for x in sentences if _NO_INFO_RE.search(x)]
    if not open_sentences:
        return "found"
    return "partial" if len(open_sentences) < len(sentences) else "not_found"


def _searched_queries(sub: dict) -> list[str]:
    return [
        q for st in sub.get("agent_steps", [])
        if st.get("action") in _SEARCH_ACTIONS
        and (q := ((st.get("args") or {}).get("query") or "").strip())
    ]


async def _pool_candidates(
    sub_query: str, query: str, global_chunks: list[dict]
) -> list[int] | None:
    """1-based pool numbers the reranker rates relevant for ``sub_query``.

    ``None`` when no reranker judgement is available (disabled, loading,
    failed) – then nobody can tell, and the caller must not treat it as
    "nothing in the pool".
    """
    candidates = [
        {"chunk_id": str(i), "metadata": {"text": c.get("context_text") or c.get("text") or ""}}
        for i, c in enumerate(global_chunks, 1)
    ]
    scored = await asyncio.to_thread(
        rerank_chunks, f"{sub_query}\n(Gesamtfrage: {query})", candidates,
        top_n=len(candidates),
    )
    if not scored or any(c["relevance_label"] == "unranked" for c in scored):
        return None
    return sorted(
        int(c["chunk_id"]) for c in scored
        if c["relevance_label"] in ("relevant", "highly_relevant")
    )


async def assess_coverage(
    query: str, subagents: list[dict], global_chunks: list[dict]
) -> list[dict]:
    """Per sub-query: status, what was searched, and – where it stayed open –
    which pool chunks could still carry it.

    Pool numbers refer to ``global_chunks`` as passed, i.e. the numbering the
    synthesizer cites against.
    """
    coverage = []
    for sub in subagents:
        status = facet_status(sub)
        entry = {
            "sub_query": sub["sub_query"],
            "status": status,
            "searched": _searched_queries(sub),
            "pool_candidates": None,
        }
        if status in ("partial", "not_found") and global_chunks and settings.COVERAGE_CHECK:
            entry["pool_candidates"] = await _pool_candidates(
                sub["sub_query"], query, global_chunks
            )
        coverage.append(entry)
    return coverage


def _coverage_line(entry: dict) -> str:
    lines = [f"STATUS: {_STATUS_LABEL[entry['status']]}"]
    if entry["searched"]:
        lines.append("Gesucht: " + "; ".join(entry["searched"]))
    candidates = entry.get("pool_candidates")
    if entry["status"] in ("partial", "not_found") and candidates is not None:
        lines.append(
            "Im Pool laut Reranker passend: " + ", ".join(f"[{n}]" for n in candidates)
            if candidates else
            "Im Pool laut Reranker passend: nichts – dieser Teil ist offen."
        )
    return "\n".join(lines)


def _format_fragments(subagents: list[dict], coverage: list[dict] | None = None) -> str:
    """Render the sub-agent answer fragments for the synthesizer prompt.

    Local ``[n]`` markers are stripped. Every sub-agent numbers its own
    observations from 1, so ``[1]`` means a different chunk in each fragment —
    and neither matches the global pool the synthesizer must cite against.
    Left in, those numbers are worse than absent: faced with three conflicting
    numbering schemes the synthesizer has produced answers with no citations at
    all. The pool is the single source of citation numbers.
    """
    lines = []
    for i, sub in enumerate(subagents):
        header = f"--- {sub['role_label']} (Sub-Query: {sub['sub_query']}) ---"
        body = (sub.get("answer") or "(leer)").strip()
        if sub.get("error"):
            body = f"(Fehler: {sub['error']})"
        else:
            body = _LOCAL_REF_RE.sub("", body).strip()
        if coverage and i < len(coverage):
            header += "\n" + _coverage_line(coverage[i])
        lines.append(f"{header}\n{body}")
    return "\n\n".join(lines)


# ── Nachrecherche: eine gezielte zweite Runde ───────────────────────────────

async def _run_gap_round(
    query: str,
    *,
    use_case: str,
    session_id: str,
    use_case_prompt: str,
    available_actions: list[str],
    collection: str,
    filters: dict | None,
    images: list[str] | None,
    image_context: str,
    subagents: list[dict],
    elapsed_s: float,
    on_event: StepCallback,
    request_id: str | None,
    username: str | None,
) -> tuple[list[dict], dict | None]:
    """Eine gezielte zweite Runde, wenn Runde 1 ein benennbares Loch liess.

    Gibt ``(zusätzliche Sub-Agents, Phasen-Eintrag)`` zurück; ohne Lücke oder
    ohne brauchbaren Vorschlag bleibt beides leer — der Normalfall.
    """
    if not settings.MANAGER_GAP_ROUND:
        return [], None
    if elapsed_s > settings.GAP_ROUND_DEADLINE_S:
        logger.info(
            "Gap round skipped: %.0fs already spent (limit %.0fs)",
            elapsed_s, settings.GAP_ROUND_DEADLINE_S,
        )
        return [], None

    global_chunks = _merge_chunks(subagents)
    coverage = await assess_coverage(query, subagents, global_chunks)
    gaps = _find_gaps(query, use_case=use_case, subagents=subagents,
                      global_chunks=global_chunks, coverage=coverage)
    if not gaps:
        return [], None

    _t0 = time.perf_counter()
    try:
        # Harte Obergrenze über Vorschlag UND Ausführung. Ohne sie hing die
        # Runde am selben langsamen LLM wie alles andere und hat die Anfrage
        # glatt verdoppelt: gemessen 87 s Nachrecherche auf 66 s Hauptlauf.
        # Lieber eine Antwort ohne Nachschlag als eine, auf die niemand wartet.
        async with asyncio.timeout(settings.GAP_ROUND_TIMEOUT_S):
            return await _gap_round_inner(
                query, use_case=use_case, session_id=session_id,
                use_case_prompt=use_case_prompt,
                available_actions=available_actions, collection=collection,
                filters=filters, images=images, image_context=image_context,
                subagents=subagents, gaps=gaps, global_chunks=global_chunks,
                started_at=_t0, on_event=on_event, request_id=request_id,
                username=username,
            )
    except asyncio.TimeoutError:
        logger.warning(
            "Gap round exceeded %.0fs — dropped, answering with round 1",
            settings.GAP_ROUND_TIMEOUT_S,
        )
        return [], None


async def _gap_round_inner(
    query: str,
    *,
    use_case: str,
    session_id: str,
    use_case_prompt: str,
    available_actions: list[str],
    collection: str,
    filters: dict | None,
    images: list[str] | None,
    image_context: str,
    subagents: list[dict],
    gaps: list[str],
    global_chunks: list[dict],
    started_at: float,
    on_event: StepCallback,
    request_id: str | None,
    username: str | None,
) -> tuple[list[dict], dict | None]:
    """Vorschlag + Ausführung der Nachrunde, unter der Zeitgrenze des Aufrufers."""
    _t0 = started_at
    new_subtasks = await propose_gap_subtasks(
        query, use_case=use_case, gaps=gaps, subagents=subagents,
        global_chunks=global_chunks,
    )
    if not new_subtasks:
        logger.info("Gap round: %d gap(s), no promising follow-up query", len(gaps))
        return [], None

    logger.info(
        "Gap round: %d gap(s) → %s",
        len(gaps), [st["sub_query"] for st in new_subtasks],
    )
    # Knappes Schritt-Budget: Die Nachrunde hat genau eine Suche plus Antwort
    # zu leisten. Wer hier nochmal frei herumsucht, kostet die Zeit, die die
    # Runde einsparen sollte.
    role = replace(get_role("facts"), max_steps=_GAP_STEP_BUDGET)
    offset = len(subagents)
    extra = await asyncio.gather(*[
        _run_one_subagent(
            sub_id=f"sub-{offset + i}-{role.name}",
            sub_query=st["sub_query"],
            user_query=query,
            role=role,
            focus=st.get("focus", ""),
            use_case=use_case,
            session_id=session_id,
            use_case_prompt=use_case_prompt,
            available_actions=available_actions,
            collection=collection,
            filters=filters,
            images=images,
            image_context=image_context,
            on_event=on_event,
            request_id=request_id,
            username=username,
        )
        for i, st in enumerate(new_subtasks, 1)
    ])
    extra = list(extra)
    phase = {
        "id": "gap_round",
        "label": "Nachrecherche",
        "ms": round((time.perf_counter() - _t0) * 1000),
        "children": subagent_phase_children(extra),
    }
    return extra, phase


#: Eine Runde, höchstens zwei Spezialisten, knappes Schritt-Budget. Mehr macht
#: aus dem Nachschlag eine zweite Anfrage — und die Wartezeit verdoppelt sich.
_MAX_GAP_SUBTASKS = 2
_GAP_STEP_BUDGET = 2
#: So viele Fundstellen aus Runde 1 gehen als Vokabelliste in den Vorschlag.
_GAP_VOCAB_CHUNKS = 8
_GAP_VOCAB_CHARS = 220

_GAP_SYSTEM = """Du planst eine gezielte Nachrecherche.

Die erste Runde Spezialisten ist gelaufen. Ein Teil der Frage ist offen
geblieben. Du siehst, WAS gefunden wurde und WO die Lücke ist.

Deine Aufgabe: höchstens 2 neue Teilfragen, die diese Lücke schliessen können.

REGELN:
- Nutze die Vokabeln aus den Fundstellen, nicht die des Nutzers. Steht dort
  "DM=63/45GRAD MESSERKOPF", suche danach — nicht nach "Dm63 Messerkopf".
- Suche woanders als Runde 1: andere Unterlagenart, anderer Begriff, andere
  Granularität. Eine Wiederholung der ersten Suche bringt dieselben Treffer.
- Verspricht keine neue Formulierung etwas, gib eine leere Liste zurück. Das
  ist die richtige Antwort, wenn der Bestand die Lücke nicht füllen kann.
- Erfinde keine Begriffe, die weder Anfrage noch Fundstellen hergeben.
- Schreibe jede Teilfrage als normalen deutschen Satz, höchstens 20 Wörter.
  KEINE Action-Aufrufe, keine Filter- oder Shell-Syntax, kein Markdown.
  Falsch: SEARCH_CNC(article=11221.5805) | grep 'Ø10'
  Richtig: Welche VHM-Fräser mit 10 mm Durchmesser nutzt Bauteil 11221.5805?

Antworte NUR mit JSON:
{"subtasks": [{"sub_query": "...", "focus": "..."}]}
"""


#: Eine sub_query, die keine Frage ist, sondern ein Aufruf. Im Betrieb kam vom
#: Planer wörtlich zurück: "SEARCH_CNC(product_family=11221.5*) | grep -i 'Ø10'".
#: So etwas geht als Suchtext in die Einbettung und trifft nichts.
_PSEUDO_QUERY_RE = re.compile(r"[|`]|\bgrep\b|[A-Z][A-Z_]{2,}\s*\(")
_MAX_GAP_QUERY_CHARS = 200


def _clean_gap_query(sub_query: str) -> str:
    """Eine brauchbare Teilfrage – oder Leerstring, wenn sie zu verwerfen ist."""
    cleaned = re.sub(r"[*#]+", "", sub_query or "").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if not cleaned or len(cleaned) > _MAX_GAP_QUERY_CHARS:
        return ""
    if _PSEUDO_QUERY_RE.search(cleaned) or len(cleaned.split()) < 3:
        return ""
    return cleaned


def _chunk_product_id(chunk: dict) -> str:
    meta = chunk.get("metadata") or {}
    extra = meta.get("extra")
    if isinstance(extra, dict) and extra.get("product_id"):
        return str(extra["product_id"])
    return str(meta.get("product_id") or "")


def _find_gaps(query: str, *, use_case: str, subagents: list[dict],
               global_chunks: list[dict],
               coverage: list[dict] | None = None) -> list[str]:
    """Konkrete Löcher nach Runde 1 – deterministisch, ohne LLM-Aufruf.

    Bewusst eng: nur ein Loch, das man benennen kann, rechtfertigt die Zeit
    einer zweiten Runde. Eine Antwort, die auf Treffern steht und einen Aspekt
    offen lässt, ist kein Loch — sie ist eine belegte Antwort.

    Eine einzelne leer ausgegangene Teilfrage zählt deshalb NUR, wenn auch
    sonst nichts gefunden wurde. Der Planer erfindet regelmässig Facetten, die
    der Bestand gar nicht führen kann („Gibt es dazu technische Zeichnungen?"),
    und im Betrieb hat das die Nachrunde bei fast jeder Anfrage ausgelöst —
    gemessen 87 s, während die übrigen Spezialisten längst geliefert hatten.
    """
    gaps: list[str] = []

    # Der teuerste Fehler im Betrieb: Die Frage nennt ein Bauteil, die Treffer
    # stammen sämtlich aus anderen. Antwortet der Synthesizer darauf, liest
    # der Rüster Werte eines fremden Teils.
    scoped = product_scope(query, use_case)
    if scoped and not any(_chunk_product_id(c) == scoped for c in global_chunks):
        gaps.append(
            f"Zu Bauteil {scoped} liegt kein einziger Treffer vor — alle "
            f"Fundstellen stammen aus anderen Bauteilen."
        )

    for sub in subagents:
        if sub.get("error"):
            gaps.append(
                f"Die Teilfrage „{sub['sub_query']}“ ist fehlgeschlagen: "
                f"{sub['error']}"
            )
        elif not sub.get("chunks") and not global_chunks:
            gaps.append(
                f"Die Teilfrage „{sub['sub_query']}“ blieb ohne Treffer."
            )

    # A sub-query the specialist left open AND the reranker finds nothing for
    # in the whole pool is a gap one can name. Open but with pool candidates
    # is not: the synthesizer can answer it from what is already there.
    for entry in coverage or []:
        if entry["status"] == "not_found" and entry.get("pool_candidates") == []:
            # Deliberately without the search terms used: they are the
            # specialist's own words and carried its misreading ("Beirut 2020")
            # straight into the gap planner's new questions.
            gaps.append(
                f"Die Teilfrage „{entry['sub_query']}“ blieb offen – weder der "
                f"Spezialist noch der gesamte Treffer-Pool enthalten dazu etwas."
            )
    return gaps


def _gap_vocabulary(global_chunks: list[dict]) -> str:
    """Die Vokabeln, die Runde 1 zutage gefördert hat.

    Der Grund, warum die Nachrunde überhaupt klüger sein kann als die blinde
    Zweitsuche im Agent-Loop: Hier steht, wie der Bestand die Sache nennt.
    """
    lines = []
    for chunk in global_chunks[:_GAP_VOCAB_CHUNKS]:
        text = " ".join((chunk.get("text") or "").split())
        if text:
            lines.append(f"- {text[:_GAP_VOCAB_CHARS]}")
    return "\n".join(lines)


async def propose_gap_subtasks(
    query: str,
    *,
    use_case: str,
    gaps: list[str],
    subagents: list[dict],
    global_chunks: list[dict],
) -> list[dict]:
    """Bis zu zwei neue Teilfragen für die Lücke – oder eine leere Liste."""
    done = {s["sub_query"].casefold() for s in subagents}
    user_parts = [
        f"Nutzer-Anfrage: {query}",
        "Offene Lücke(n):",
        *(f"- {g}" for g in gaps),
        "Bereits gestellte Teilfragen (nicht wiederholen):",
        *(f"- {s['sub_query']}" for s in subagents),
    ]
    if vocab := _gap_vocabulary(global_chunks):
        user_parts += ["Fundstellen aus Runde 1 (Vokabular des Bestands):", vocab]

    try:
        raw = await call_llm(
            [{"role": "system", "content": _GAP_SYSTEM},
             {"role": "user", "content": "\n".join(user_parts)}],
            use_case=use_case,
            max_tokens=TOKENS_PLAN,
        )
    except Exception as exc:
        logger.warning(f"Gap-round planner LLM failed: {exc}")
        return []

    parsed = _extract_json(raw) or {}
    proposed = parsed.get("subtasks")
    if not isinstance(proposed, list):
        return []

    cleaned: list[dict] = []
    for st in proposed:
        if not isinstance(st, dict):
            continue
        sub_query = _clean_gap_query(strip_invented_years(
            _strip_placeholders((st.get("sub_query") or "").strip(), query),
            "\n".join([query, *(c.get("text") or "" for c in global_chunks)]),
        ))
        if not sub_query or sub_query.casefold() in done:
            continue
        done.add(sub_query.casefold())
        cleaned.append({
            "role": "facts",
            "sub_query": sub_query,
            "focus": (st.get("focus") or "").strip(),
        })
        if len(cleaned) == _MAX_GAP_SUBTASKS:
            break
    return cleaned


async def synthesize(
    *,
    query: str,
    plan: dict,
    subagents: list[dict],
    use_case: str,
    on_event: StepCallback,
    compliance_guidance: str = "",
    coverage: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Merge sub-agent fragments into one answer + return the global chunk pool.

    Returns ``(final_answer, global_chunks)``. ``global_chunks`` is the
    deduped, globally numbered pool – the caller uses it for citation mapping.
    """
    global_chunks = _merge_chunks(subagents)

    # Degenerate cases: nothing found at all, or only one sub-agent and
    # it gave a sufficient answer. Skip the LLM call — no value to add.
    if not global_chunks:
        if on_event:
            await on_event({"type": "synthesizer", "phase": "skipped",
                            "reason": "no_chunks"})
        # Reuse the most informative sub-agent answer
        fallback = next(
            (s["answer"] for s in subagents if s.get("answer")),
            "Zu dieser Anfrage wurden keine passenden Dokumente gefunden.",
        )
        return fallback, []

    if on_event:
        await on_event({
            "type": "synthesizer",
            "phase": "started",
            "merge_strategy": plan["merge_strategy"],
            "fragment_count": len(subagents),
            "global_chunk_count": len(global_chunks),
        })

    available_images = _collect_available_images(global_chunks)

    system = _SYNTHESIZER_SYSTEM_TEMPLATE.format(strategy=plan["merge_strategy"])
    guidance_block = (
        f"\nKORREKTUR-HINWEIS DES COMPLIANCE-AGENTEN:\n{compliance_guidance}\n"
        "Berücksichtige diesen Hinweis beim Umformulieren.\n"
    ) if compliance_guidance else ""
    form_block = (
        f"ANTWORTFORM: {_ANSWER_FORMS[plan['answer_form']]}\n\n"
        if plan.get("answer_form") in _ANSWER_FORMS else ""
    )
    user = (
        f"ORIGINALFRAGE:\n{query}\n\n"
        f"{form_block}"
        f"PLAN-RATIONALE (Manager):\n{plan.get('rationale', '')}\n\n"
        f"ANTWORT-FRAGMENTE DER SPEZIALISTEN (ohne Quellennummern — die "
        f"Nummern vergibst du selbst aus dem GLOBAL_CHUNK_POOL):\n"
        f"{_format_fragments(subagents, coverage)}\n\n"
        f"GLOBAL_CHUNK_POOL (für [n]-Verweise — gültig sind ausschliesslich "
        f"[1] bis [{len(global_chunks)}]):\n"
        f"{_format_global_chunks(global_chunks)}\n\n"
        f"AVAILABLE_IMAGES (für [BILD: <id>]-Verweise):\n"
        f"{_format_available_images(available_images)}\n"
        f"{guidance_block}\n"
        # Citation anchor in the highest-recency position. The rule also
        # stands in the system prompt, but the chunk pool now sits between the
        # two and is by far the longest block in the request: twice in a row
        # the synthesizer returned an answer without a single [n], which the
        # deterministic check caught and repaired with a second synthesis call
        # — and that repair, having to invent numbers after the fact, attached
        # a statement from page 72 to the cover page of an unrelated document.
        # Cheaper and more reliable to state the requirement where the model
        # starts generating than to correct it afterwards.
        + "Formuliere jetzt die finale Antwort. Setze hinter JEDE einzelne "
        f"Tatsachenbehauptung die Nummer [n] des Chunks aus dem "
        f"GLOBAL_CHUNK_POOL, aus dem sie stammt (gültig: [1] bis "
        f"[{len(global_chunks)}]). Eine Antwort ohne [n] im Fliesstext ist "
        "ungültig und wird verworfen."
    )

    try:
        final = await call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            use_case=use_case,
            max_tokens=TOKENS_SYNTHESIS,
        )
    except Exception as exc:
        logger.warning(f"Synthesizer LLM failed: {exc} — falling back to concatenation")
        final = _concat_fragments_fallback(subagents)

    final = _clean_synthesized(final)
    if on_event:
        await on_event({"type": "synthesizer", "phase": "done",
                        "answer_length": len(final)})
    return final, global_chunks


def _grounding_sources(query: str, global_chunks: list[dict]) -> list[str]:
    return [query, *(c.get("context_text") or c.get("text") or "" for c in global_chunks)]


async def _ground_answer(
    answer: str, *, query: str, plan: dict, subagents: list[dict],
    global_chunks: list[dict], use_case: str, on_event: StepCallback,
    coverage: list[dict] | None,
) -> dict:
    """Check the answer against the pool; rewrite once if something is unbacked.

    Returns ``{"checked", "found", "remaining", "rewritten", "ms"}`` and, when
    rewritten, ``"answer"`` with the new text. A figure the answer computed
    from backed figures (a sum, a difference) is legitimate; the rewrite is
    told to keep those and show the calculation.
    """
    empty = {"numbers": [], "names": []}
    if not settings.GROUNDING_CHECK or not global_chunks or not answer:
        return {"checked": False}
    started = time.perf_counter()
    sources = _grounding_sources(query, global_chunks)
    found = find_ungrounded(answer, sources)
    result = {"checked": True, "found": found, "remaining": empty, "rewritten": False}
    # Years without a source are removed directly – a rewrite request for
    # them was ignored more often than followed ("(4. August 2020)" stayed).
    # The same guard the search queries get; see core.guards.
    unyeared = strip_invented_years(answer, "\n".join(sources))
    if unyeared != answer:
        answer = unyeared
        result.update(answer=answer, rewritten=True)
        found = find_ungrounded(answer, sources)
        result["remaining"] = found
    if found["numbers"] or found["names"]:
        items = [*(f"Zahl „{n}“" for n in found["numbers"]),
                 *(f"Name „{n}“" for n in found["names"])]
        guidance = (
            "Diese Angaben der vorigen Fassung stehen in KEINER Quelle: "
            + ", ".join(items) + ". Streiche sie. Ausnahme: eine Zahl, die du aus "
            "belegten Zahlen berechnet hast – dann behalte sie und zeige die "
            "Rechnung mit den Quellen der Ausgangszahlen."
        )
        logger.info("Grounding check: unbacked %s – rewriting", items)
        rewritten, _ = await synthesize(
            query=query, plan=plan, subagents=subagents, use_case=use_case,
            on_event=on_event, compliance_guidance=guidance, coverage=coverage,
        )
        if rewritten:
            result.update(answer=rewritten, rewritten=True,
                          remaining=find_ungrounded(rewritten, sources))
    result["ms"] = round((time.perf_counter() - started) * 1000)
    return result


def _concat_fragments_fallback(subagents: list[dict]) -> str:
    """If the synthesizer LLM fails, glue fragments together verbatim.

    Citation numbering will be wrong (local-per-sub-agent), but the user
    still gets the underlying information instead of a hard failure.
    """
    parts = []
    for sub in subagents:
        if not sub.get("answer"):
            continue
        parts.append(f"**{sub['role_label']}:** {sub['answer']}")
    if not parts:
        return "Es konnten keine belastbaren Informationen gefunden werden."
    return "\n\n".join(parts)



async def _classify_via_llm(issues: list[str]) -> list[dict]:
    """Classify unmatched issues with llama.cpp."""
    import httpx

    if not issues:
        return []

    # Build numbered issue list for the prompt
    issues_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(issues))

    system_prompt = (
        "Du bist ein Klassifizierer fuer Compliance-Probleme eines RAG-Systems. "
        "Ordne jedes Problem GENAU EINER der folgenden Kategorien zu:\n\n"
        "1. ZITIERABDECKUNG - fehlende oder unzureichende Quellenangaben/Zitationen\n"
        "2. QUELLENECHTHEIT - zitierte Quellen belegen die Aussage nicht oder widersprechen ihr\n"
        "3. HALLUZINATION - erfundene Fakten, die in keinen Chunks existieren\n"
        "4. USE_CASE_RICHTLINIE - Verstoss gegen Format-, Laengen-, Relevanz- oder Ton-Vorgaben\n\n"
        "Antworte NUR mit JSON-Array, kein Markdown, kein Vor-/Nachspann:\n"
        '[{"issue": 1, "category": "ZITIERABDECKUNG"}, ...]'
    )

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": issues_text},
        ],
        "temperature": 0.0,
        "max_tokens": 256,
        "model": "Qwen3.5-0.8B",
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "http://172.19.0.1:8081/v1/chat/completions",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"]

    # Parse JSON response
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON array via regex
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
        else:
            logger.warning(f"LLM classification parse failed: {raw[:200]}")
            return []

    if not isinstance(parsed, list):
        return []

    # Map LLM categories back to our keys
    CAT_MAP = {
        "ZITIERABDECKUNG": "citation_coverage",
        "QUELLENECHTHEIT": "source_authenticity",
        "HALLUZINATION": "hallucination",
        "USE_CASE_RICHTLINIE": "use_case_policy",
    }

    results = []
    for item in parsed:
        cat = item.get("category", "").upper().strip()
        cat_key = None
        for llm_cat, our_key in CAT_MAP.items():
            if llm_cat in cat:
                cat_key = our_key
                break

        results.append({
            "text": issues[item.get("issue", 1) - 1] if item.get("issue") else "",
            "category": cat_key or "unknown",
            "category_label": _CATEGORY_LABELS.get(cat_key or "unknown", "Unbekannt"),
            "confidence": 70.0,
            "nli_label": "LLM",
        })

    return results


async def _classify_compliance_issues(issues: list[str]) -> list[dict]:
    """Classify each compliance issue. Keywords first, llama.cpp fallback if no match."""
    if not issues:
        return []

    results: list[dict] = []
    unknown_indices: list[int] = []

    # Try keyword matching first
    for i, issue_text in enumerate(issues):
        text_lower = issue_text.lower()
        scores: dict[str, int] = {}
        for cat_key, keywords in _COMPLIANCE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            scores[cat_key] = score

        best_cat = max(scores, key=scores.get)
        best_score = scores[best_cat]

        if best_score >= 1:
            conf = 60.0 if best_score == 1 else 80.0 if best_score == 2 else 95.0
            results.append({
                "text": issue_text,
                "category": best_cat,
                "category_label": _CATEGORY_LABELS.get(best_cat, "Unbekannt"),
                "confidence": conf,
                "nli_label": "KEYWORD",
            })
        else:
            unknown_indices.append(i)
            results.append({})  # placeholder

    # Use LLM for anything keywords missed
    if unknown_indices:
        unknown_issues = [issues[idx] for idx in unknown_indices]
        try:
            llm_results = await _classify_via_llm(unknown_issues)
        except Exception:
            logger.warning("LLM classification fallback failed")
            llm_results = []

        for j, idx in enumerate(unknown_indices):
            if j < len(llm_results) and llm_results[j] and llm_results[j].get("category") != "unknown":
                results[idx] = llm_results[j]
            else:
                results[idx] = {
                    "text": issues[idx],
                    "category": "unknown",
                    "category_label": "Unbekannt",
                    "confidence": 0.0,
                    "nli_label": "LLM_FAILED",
                }

    return results


#: A claim attributed to a skill: "laut Skill wetter", "Laut **Skill** wetter".
#: Markdown emphasis lands between the words often enough that ignoring it
#: would make the check trivially easy to slip past.
_SKILL_ATTRIBUTION_RE = re.compile(
    r"laut\s+[*_`]{0,2}\s*Skill\b", re.IGNORECASE
)


async def check_compliance(
    *,
    answer: str,
    global_chunks: list[dict],
    use_case_prompt: str,
    use_case: str,
    on_event: StepCallback,
    executed_skills: list[str] | None = None,
) -> dict:
    """Run a compliance check on the synthesized answer.

    Returns a dict with ``verdict``, ``issues``, ``guidance``. Always returns
    something usable — on LLM/parse error the verdict is ``OK`` (don't block
    answers because the checker itself broke).
    """
    if not answer.strip():
        return {"verdict": "OK", "issues": [], "classified_issues": [], "guidance": ""}

    if on_event:
        await on_event({"type": "compliance", "phase": "started"})

    # Deterministic pre-check. "Citation-Coverage" is the checker's own first
    # criterion, but it has passed answers whose only [n] sat in a trailing
    # source list — form satisfied, purpose missed. A citation belongs next to
    # the statement it supports; collected at the end it tells the reader
    # nothing about which claim rests on what. No judgement needed, so this is
    # decided in code rather than asked of the model.
    # A skill call is a citation of a different kind, so an answer carrying one
    # is not "unsourced" — it is sourced by something that has not run yet.
    # Without this the deterministic check below rewrites exactly the answers
    # the plugin layer is supposed to produce.
    skill_calls: list = []
    try:
        known = {f.skill_name.lower() for f in await list_skill_functions(use_case)}
        if known:
            skill_calls = find_calls(answer, known_skills=known)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not check the answer for skill calls: %s", exc)

    # A source that was never consulted is the worst kind of citation: it reads
    # as evidence and is not. Teaching the synthesiser that "laut Skill X" is a
    # valid attribution also taught it the phrase — and it used it for a weather
    # claim while no skill had run. The checker passed that as OK, so this is
    # decided in code: whether a function ran is a fact, not a judgement.
    if _SKILL_ATTRIBUTION_RE.search(answer) and not executed_skills:
        issue = (
            "Die Antwort schreibt eine Aussage einem Skill zu („laut Skill …“), "
            "obwohl für diese Anfrage keine Skill-Funktion ausgeführt wurde. "
            "Der Wert ist damit frei erfunden."
        )
        logger.warning("Compliance short-circuit: fabricated skill attribution")
        result = {
            "verdict": "REWRITE",
            "issues": [issue],
            "classified_issues": [],
            "guidance": (
                "Entferne jede Aussage, die sich auf einen Skill beruft, und "
                "jeden daraus abgeleiteten Wert. Es wurde keine Skill-Funktion "
                "ausgeführt. Sage stattdessen ausdrücklich, dass die "
                "Information nicht vorliegt."
            ),
        }
        if on_event:
            await on_event({"type": "compliance", "phase": "done", **result})
        return result

    if (
        global_chunks
        and not has_inline_citations(answer)
        and not skill_calls
        and not executed_skills
    ):
        issue = (
            "Die Antwort belegt keine einzige Aussage im Fliesstext mit [n] "
            f"(Chunk-Pool: {len(global_chunks)} Chunks). Verweise stehen "
            "gar nicht oder nur in einer Quellenliste am Ende."
        )
        logger.warning("Compliance short-circuit: %s", issue)
        result = {
            "verdict": "REWRITE",
            "issues": [issue],
            "classified_issues": [],
            "guidance": (
                "Setze die Quellennummer [1] bis "
                f"[{len(global_chunks)}] direkt hinter die jeweilige Aussage "
                "im Fliesstext. Keine gesammelte Quellenliste am Ende."
            ),
        }
        if on_event:
            await on_event({"type": "compliance", "phase": "done", **result})
        return result

    skill_note = ""
    if skill_calls:
        listed = ", ".join(sorted({c.label() for c in skill_calls}))
        skill_note = (
            "\nHINWEIS — die Antwort enthält diese Skill-Funktionsaufrufe: "
            f"{listed}\nDas sind Platzhalter, die nach deiner Prüfung durch "
            "echte Werte ersetzt werden. Sie sind belegt und brauchen kein "
            "[n]. Beanstande sie nicht.\n"
        )
    if executed_skills:
        skill_note += (
            "\nHINWEIS — für diese Antwort wurden Skill-Funktionen ausgeführt: "
            f"{', '.join(executed_skills)}\nDie Werte daraus stehen bereits im "
            "Text und stammen aus einer definierten Quelle, nicht vom Modell. "
            "Sie stehen in KEINEM Chunk — das ist erwartet — und brauchen kein "
            "[n]. Eine Antwort, die nur aus solchen Werten besteht, ist „OK“, "
            "auch wenn der Chunk-Pool leer ist.\n"
        )
    user = (
        f"USE-CASE-POLICY (aus dem System-Prompt):\n{use_case_prompt}\n\n"
        f"GLOBAL_CHUNK_POOL:\n{_format_global_chunks(global_chunks)}\n\n"
        f"FINALE ANTWORT:\n{answer}\n"
        f"{skill_note}\n"
        "Beurteile die Antwort."
    )
    try:
        raw = await call_llm(
            [{"role": "system", "content": _COMPLIANCE_SYSTEM},
             {"role": "user", "content": user}],
            use_case=use_case,
            max_tokens=TOKENS_COMPLIANCE,
        )
    except Exception as exc:
        logger.warning(f"Compliance LLM failed: {exc} — passing through")
        if on_event:
            await on_event({"type": "compliance", "phase": "error", "detail": str(exc)})
        return {"verdict": "OK", "issues": [], "classified_issues": [], "guidance": ""}

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        if on_event:
            await on_event({"type": "compliance", "phase": "done",
                            "verdict": "OK", "issues": [], "classified_issues": [], "guidance": ""})
        return {"verdict": "OK", "issues": [], "guidance": ""}

    verdict = parsed.get("verdict") if parsed.get("verdict") in ("OK", "REWRITE", "REFUSE") else "OK"
    issues = parsed.get("issues") if isinstance(parsed.get("issues"), list) else []
    guidance = parsed.get("guidance") if isinstance(parsed.get("guidance"), str) else ""

    issues, unfounded = _drop_unfounded_issues(issues, global_chunks, answer)
    if unfounded:
        logger.warning(
            "Compliance flagged %d claim(s) that stand verbatim in the chunk "
            "pool — dropped: %s",
            len(unfounded),
            " | ".join(i[:120] for i in unfounded),
        )
        # Nothing left to act on: rewriting would reassign every citation
        # number for no correction.
        if not issues:
            verdict = "OK" if verdict != "REFUSE" else verdict
            guidance = ""

    # Classify issues via NLI if any
    # Classify issues via NLI if any (with fallback — don't crash compliance on NLI failure)
    classified_issues = await _classify_compliance_issues(issues) if issues else []

    result = {"verdict": verdict, "issues": issues, "classified_issues": classified_issues, "guidance": guidance}
    if on_event:
        await on_event({"type": "compliance", "phase": "done", **result})
    return result


# --- Deterministic guard against a compliance false positive ----------------
#
# The compliance model flagged as a hallucination a sentence standing verbatim
# in the chunk it cited ("Ändert das Fahrpersonal manuell die Ausstiegsseite,
# ändert sich ebenfalls das Aussehen der Pfeile"). It sees the full pool, so it
# had the text in front of it. A wrong REWRITE is not free: it spends a second
# synthesis pass during which the synthesizer reassigns every citation number,
# and one such pass has already moved a correct statement onto an unrelated
# document's cover page.
#
# Whether a run of words appears in the pool needs no judgement, so — like the
# inline-citation check above — it is decided in code.

_ISSUE_QUOTE_RE = re.compile(r"[\'\"„“”‚‘’«»]([^\'\"„“”‚‘’«»]{20,400})[\'\"„“”‚‘’«»]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Consecutive words of a flagged claim that must stand in the pool for the
# claim to count as grounded. Long enough that ordinary German connective
# phrasing cannot reach it by chance, short enough to survive the real
# mismatches: a chunk cut mid-word at its boundary ("Pfeil" for "Pfeile"), a
# dropped article, an inserted citation marker.
_VERBATIM_RUN_WORDS = 8


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _longest_shared_run(claim: list[str], pool: list[str]) -> int:
    """Longest run of consecutive *claim* words occurring in order in *pool*."""
    if not claim or not pool:
        return 0
    best = 0
    positions: dict[str, list[int]] = {}
    for i, w in enumerate(pool):
        positions.setdefault(w, []).append(i)
    for start in range(len(claim)):
        if len(claim) - start <= best:
            break  # no remaining window can beat the best run
        for pos in positions.get(claim[start], ()):
            run = 0
            while (start + run < len(claim) and pos + run < len(pool)
                   and claim[start + run] == pool[pos + run]):
                run += 1
            best = max(best, run)
    return best


def _issue_is_contradicted_by_the_pool(issue: str, pool_words: list[str]) -> bool:
    """True if a claim the issue quotes stands verbatim in the chunk pool."""
    for quoted in _ISSUE_QUOTE_RE.findall(issue):
        # Citation markers are the answer's, not the source's.
        claim = _words(re.sub(r"\[\d+]", " ", quoted))
        if len(claim) >= _VERBATIM_RUN_WORDS and (
            _longest_shared_run(claim, pool_words) >= _VERBATIM_RUN_WORDS
        ):
            return True
    return False


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Issue wordings that assert the answer contains something the sources do not.
# Only these are refutable by showing the content is in fact there; a
# citation-coverage or policy complaint is about form and stands regardless.
_CONTENT_CLAIM_MARKERS = (
    "halluzin", "erfunden", "fiktiv", "stammt nicht", "steht in keinem",
    "nicht im chunk", "nicht aus dem pool", "kein chunk", "nicht belegt",
    "belegt nicht", "bestätigt nicht", "bestaetigt nicht", "stützt nicht",
    "stuetzt nicht", "aber nicht", "nicht vorhanden", "nicht gedeckt",
)


def _ungrounded_sentences(answer: str, global_chunks: list[dict]) -> list[str]:
    """Answer sentences whose wording is not verbatim in the chunk THEY cite.

    Checking against the cited chunk rather than the whole pool is what keeps
    this from excusing a real mis-citation: an answer that attaches a true
    statement to the wrong source has its content in the pool but not in the
    chunk it names, and stays flagged.

    Sentences without a citation are skipped — that is the inline-citation
    check's job — as are sentences too short for a run of this length to be
    meaningful.
    """
    ungrounded: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(answer or ""):
        refs = [int(n) for n in re.findall(r"\[(\d+)]", sentence)]
        cited = [global_chunks[n - 1] for n in refs if 1 <= n <= len(global_chunks)]
        if not cited:
            continue
        claim = _words(re.sub(r"\[\d+]", " ", sentence))
        if len(claim) < _VERBATIM_RUN_WORDS:
            continue
        source = _words(" ".join((c.get("text") or "") for c in cited))
        if _longest_shared_run(claim, source) < _VERBATIM_RUN_WORDS:
            ungrounded.append(sentence.strip())
    return ungrounded


def _drop_unfounded_issues(
    issues: list[str], global_chunks: list[dict], answer: str = ""
) -> tuple[list[str], list[str]]:
    """Split *issues* into (kept, dropped-as-contradicted-by-the-sources).

    Two passes, because the model states the same false finding in several
    ways. The first drops an issue that quotes a claim standing verbatim in the
    pool. The second is what actually settles it: when no sentence of the
    answer is ungrounded against its own citation, an issue asserting the
    answer contains something the sources do not is false by construction —
    including the restatements that quote nothing and would otherwise survive.
    """
    if not issues or not global_chunks:
        return issues, []

    pool_words = _words(" ".join((c.get("text") or "") for c in global_chunks))
    kept, dropped = [], []
    for issue in issues:
        (dropped if _issue_is_contradicted_by_the_pool(issue, pool_words) else kept).append(issue)

    if kept and answer and not _ungrounded_sentences(answer, global_chunks):
        still_kept = []
        for issue in kept:
            low = issue.lower()
            if any(marker in low for marker in _CONTENT_CLAIM_MARKERS):
                dropped.append(issue)
            else:
                still_kept.append(issue)
        kept = still_kept

    return kept, dropped


_SYNTH_CLEANUP_RE = re.compile(
    r"^\s*(?:FINAL_ANSWER|ANTWORT)\s*[:(]\s*",
    re.IGNORECASE,
)


# The pipeline's own vocabulary, as it leaks into answers ("… da der
# GLOBAL_CHUNK_POOL hierzu keine Angaben enthält"). The prompt forbids it; the
# reader must not see it either way. Longest patterns first.
_INTERNAL_TERMS = [
    # Replacements keep the grammatical gender, so the surrounding article
    # and case stay right ("der GLOBAL_CHUNK_POOL" → "der Quellenbestand").
    (re.compile(r"\bGLOBAL_CHUNK_POOL\b|\b(?:Chunk|Treffer)-Pool\b|\bPool\b"), "Quellenbestand"),
    (re.compile(r"\b(den|aus|in|mit|von|zu)\s+(?:den\s+)?Chunks\b"),
     lambda m: m.group(0).replace("Chunks", "Textabschnitten")),
    (re.compile(r"\bChunks\b"), "Textabschnitte"),
    (re.compile(r"\bChunk\b"), "Textabschnitt"),
    (re.compile(r"\bFragmente\s+der\s+Spezialisten\b"), "Teilergebnisse"),
    (re.compile(r"\b(?:Antwort-)?Fragmente\b"), "Teilergebnisse"),
    (re.compile(r"\b(?:Antwort-)?Fragment\b"), "Teilergebnis"),
]


def _clean_synthesized(text: str) -> str:
    """Trim synthesizer output of common artefacts (leading labels, fences,
    the pipeline's internal vocabulary)."""
    text = text.strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = _SYNTH_CLEANUP_RE.sub("", text)
    for pattern, replacement in _INTERNAL_TERMS:
        text = pattern.sub(replacement, text)
    return text.strip(' "\n')


# ── Chunk similarity enrichment ─────────────────────────────────────────────

async def _enrich_chunk_similarities(subagents: list[dict], global_chunks: list[dict], final_answer: str) -> None:
    """Compute similarity_to_rank_1 and answer_similarity for every chunk.

    Calls the embedding service once for all chunk texts + the final answer,
    then computes cosine similarity (dot product on normalised vectors) and
    stores the results directly on each chunk dict in every sub-agent step.
    """
    import httpx
    import numpy as np

    if not final_answer.strip():
        return

    # Collect every chunk from every step. We track (text → list of chunk
    # dicts) so we can update duplicates in-place after embedding.
    text_to_chunks: dict[str, list[dict]] = {}
    all_texts: list[str] = []

    for sub in subagents:
        for step in sub.get("agent_steps", []):
            for chunk in step.get("chunks") or []:
                text = (chunk.get("text") or "").strip()
                if not text:
                    continue
                if text in text_to_chunks:
                    text_to_chunks[text].append(chunk)
                else:
                    text_to_chunks[text] = [chunk]
                    all_texts.append(text)

    # Also collect from global_chunks (used by SYNTHESIZE step)
    for chunk in global_chunks:
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        if text in text_to_chunks:
            text_to_chunks[text].append(chunk)
        else:
            text_to_chunks[text] = [chunk]
            all_texts.append(text)

    if not all_texts:
        return

    # Build batch: chunk texts + final answer as the last entry
    batch = [{"chunk_id": str(i), "content": t} for i, t in enumerate(all_texts)]
    answer_idx = len(batch)
    batch.append({"chunk_id": "answer", "content": final_answer})

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "http://embedding:8003/v1/embed/batch",
                json={"chunks": batch},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning(f"Chunk similarity enrichment failed (embedding): {exc}")
        return

    embeddings_list: list = data.get("embeddings", [])
    if len(embeddings_list) != len(batch):
        logger.warning(
            "Chunk similarity enrichment: embedding count mismatch "
            f"({len(embeddings_list)} vs {len(batch)})"
        )
        return

    # Build text → normalised vector map
    text_to_vec: dict[str, "np.ndarray"] = {}
    for i, emb in enumerate(embeddings_list):
        vec_raw = emb.get("vector", [])
        if not vec_raw:
            continue
        vec = np.array(vec_raw, dtype=np.float64)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        text_to_vec[batch[i]["content"]] = vec

    answer_vec = text_to_vec.get(final_answer)
    if answer_vec is None:
        return

    # Rank-1 = chunk with the highest rerank_score (not just first in iteration)
    # -inf, not -1.0: cross-encoder logits are routinely below -1, and a pool
    # of only weak chunks would otherwise leave no rank-1 chunk at all.
    _best_score = float("-inf")
    rank1_text: str | None = None
    for text in all_texts:
        for chunk in text_to_chunks.get(text, []):
            s = chunk.get("rerank_score") or chunk.get("score") or 0.0
            if s > _best_score:
                _best_score = s
                rank1_text = text
    rank1_vec = text_to_vec.get(rank1_text) if rank1_text else None

    for text, vec in text_to_vec.items():
        if text == final_answer:
            continue
        sim_r1 = round(float(np.dot(vec, rank1_vec)), 4) if rank1_vec is not None else None
        sim_ans = round(float(np.dot(vec, answer_vec)), 4)
        for chunk in text_to_chunks.get(text, []):
            chunk["similarity_to_rank_1"] = sim_r1
            chunk["answer_similarity"] = sim_ans


# ── Top-level entry point ───────────────────────────────────────────────────


async def run_manager(
    query: str,
    *,
    use_case: str,
    session_id: str,
    use_case_prompt: str,
    available_actions: list[str],
    collection: str = "",
    filters: dict | None = None,
    history: list[dict] | None = None,
    images: list[str] | None = None,
    request_id: str | None = None,
    username: str | None = None,
    on_event: StepCallback = None,
) -> dict:
    """Top-level orchestration: plan → fan-out → synthesize.

    Return shape is a superset of ``run_agent``'s return so the router /
    persistence layer can stay roughly compatible:

      answer, citations, sufficient, searched_collections, session_id,
      use_case  – unchanged semantics
      manager_plan        – the decomposition decided by the manager
      subagents           – list of per-sub-agent traces (role, sub_query,
                            answer, agent_steps, chunks, sufficient, error)
      agent_steps         – flattened compatibility view: manager event +
                            each sub-agent's steps + synthesizer event
      global_chunks       – deduped chunk pool used for citation mapping
    """
    _t0 = time.perf_counter()
    phases: list[dict] = []
    if on_event:
        await on_event({"type": "started", "stage": "manager"})

    # If the user attached image(s), describe them with the vision model first
    # so the content is available as text to the planner and every sub-agent
    # (works regardless of whether the chat model is multimodal).
    image_context = ""
    if images:
        if on_event:
            await on_event({"type": "image_analysis", "count": len(images)})
        _img_t = time.perf_counter()
        image_context = await describe_uploaded_images(images, use_case=use_case)
        phases.append({
            "id": "images",
            "label": "Bildanalyse",
            "ms": round((time.perf_counter() - _img_t) * 1000),
        })

    # Vague/incomplete query → recall the most relevant past Q&A from the
    # memory RAG so the manager can plan concrete sub-tasks instead of guessing.
    # Gated on vagueness so clear queries pay no extra embed+search cost.
    memory = ""
    if _is_vague_query(query, history):
        _mem_t = time.perf_counter()
        mems = await recall(use_case, query)
        if mems:
            if on_event:
                await on_event({"type": "memory_recall", "count": len(mems)})
            memory = format_memory(mems)
            phases.append({
                "id": "memory",
                "label": "Gedächtnis-Suche",
                "ms": round((time.perf_counter() - _mem_t) * 1000),
            })

    # What this use case can do beyond searching documents. The planner needs
    # it, and so do the sub-agents: SKILL exists exactly when there are
    # functions to call, so nobody has to switch it on separately.
    try:
        skill_functions = await list_skill_functions(use_case)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not load skill functions for {use_case}: {exc}")
        skill_functions = []
    if skill_functions and "SKILL" not in available_actions:
        available_actions = [*available_actions, "SKILL"]

    _plan_t = time.perf_counter()
    plan = await plan_subtasks(
        query, use_case=use_case, history=history,
        image_context=image_context, memory=memory,
        domain_prompt=use_case_prompt,
        skill_functions=skill_functions,
    )
    _plan_llm_timing = get_last_llm_timing()
    _plan_dur = round((time.perf_counter() - _plan_t) * 1000)
    phases.append({
        "id": "plan",
        "label": "Planung (Manager)",
        "ms": _plan_dur,
        "llm_ms": (_plan_llm_timing or {}).get("total_ms"),
    })
    if on_event:
        await on_event({"type": "manager_plan", **plan})

    # Fan-out — run sub-agents in parallel. Each gets a stable ``sub_id``
    # used by the frontend to group their events.
    # Reset timing: sub-agents will fill it with their own LLM calls;
    # we want the synthesize timing to be clean afterwards.
    subagent_coros = []
    scoped_product = product_scope(query, use_case)
    for i, st in enumerate(plan["subtasks"], 1):
        role = get_role(st["role"])
        sub_id = f"sub-{i}-{role.name}"
        subagent_coros.append(
            _run_one_subagent(
                sub_id=sub_id,
                sub_query=_with_product_scope(st["sub_query"], scoped_product),
                user_query=query,
                role=role,
                focus=st.get("focus", ""),
                use_case=use_case,
                session_id=session_id,
                use_case_prompt=use_case_prompt,
                available_actions=available_actions,
                collection=collection,
                filters=filters,
                images=images,
                image_context=image_context,
                on_event=on_event,
                request_id=request_id,
                username=username,
            )
        )
    _subs_t = time.perf_counter()
    subagents = list(await asyncio.gather(*subagent_coros))
    _subs_dur = round((time.perf_counter() - _subs_t) * 1000)
    phases.append({
        "id": "subagents",
        "label": "Sub-Agents (parallel)",
        "ms": _subs_dur,
        "children": subagent_phase_children(subagents),
    })

    # Blieb ein benennbares Loch, darf genau eine gezielte Runde nachlegen —
    # mit den Vokabeln aus Runde 1. Das ist der Platz dafür: Der Manager sieht
    # alle Fragmente, ein einzelner Spezialist immer nur seinen Ausschnitt.
    gap_subagents, gap_phase = await _run_gap_round(
        query,
        use_case=use_case,
        session_id=session_id,
        use_case_prompt=use_case_prompt,
        available_actions=available_actions,
        collection=collection,
        filters=filters,
        images=images,
        image_context=image_context,
        subagents=subagents,
        elapsed_s=time.perf_counter() - _t0,
        on_event=on_event,
        request_id=request_id,
        username=username,
    )
    if gap_subagents:
        subagents += gap_subagents
        phases.append(gap_phase)

    # Synthesize
    _synth_start = time.perf_counter()
    # Same numbering synthesize() derives from the same sub-agents.
    coverage = await assess_coverage(query, subagents, _merge_chunks(subagents))
    reset_llm_timing()  # guard against synthesize skipping the LLM call
    final_answer, global_chunks = await synthesize(
        query=query,
        plan=plan,
        subagents=subagents,
        use_case=use_case,
        on_event=on_event,
        coverage=coverage,
    )
    _synth_llm_timing = get_last_llm_timing()
    _synth_dur = round((time.perf_counter() - _synth_start) * 1000)
    phases.append({
        "id": "synthesize",
        "label": "Synthese",
        "ms": _synth_dur,
        "llm_ms": (_synth_llm_timing or {}).get("total_ms"),
    })

    # Grounding: figures and names the answer states must be in what was
    # retrieved. One targeted rewrite if not – the compliance LLM waved such
    # answers through ("Calvert-Lewin" in no chunk; "2020" in no chunk).
    grounding = await _ground_answer(
        final_answer, query=query, plan=plan, subagents=subagents,
        global_chunks=global_chunks, use_case=use_case, on_event=on_event,
        coverage=coverage,
    )
    if grounding.get("rewritten"):
        final_answer = grounding.pop("answer")
        phases.append({"id": "grounding", "label": "Belegprüfung (Überarbeitung)",
                       "ms": grounding["ms"]})

    # Compliance check + optional one-shot rewrite
    _compliance_start = time.perf_counter()
    reset_llm_timing()  # guard against compliance skipping the LLM call
    # Which functions the sub-agents actually ran. With the SKILL action the
    # call no longer appears in the answer text — the value is already
    # substituted — so scanning the text for it finds nothing, and a pure
    # live-data answer looked entirely unsourced to the checker.
    executed_skills = sorted({
        f"{(st.get('args') or {}).get('skill', '?')}."
        f"{(st.get('args') or {}).get('function', '?')}"
        for sub in subagents
        for st in (sub.get("agent_steps") or [])
        if st.get("action") == "SKILL"
    })

    compliance = await check_compliance(
        answer=final_answer,
        global_chunks=global_chunks,
        use_case_prompt=use_case_prompt,
        use_case=use_case,
        executed_skills=executed_skills,
        on_event=on_event,
    )
    _compliance_llm_timing = get_last_llm_timing()
    _compliance_dur = round((time.perf_counter() - _compliance_start) * 1000)
    phases.append({
        "id": "compliance",
        "label": "Compliance",
        "ms": _compliance_dur,
        "llm_ms": (_compliance_llm_timing or {}).get("total_ms"),
    })
    # Fallback: when the LLM call fails (e.g. Ollama 504), _capture_llm_timing
    # was never called and get_last_llm_timing() returns {}. Use wall-clock
    # duration so the UI still shows timing instead of nothing.
    if not _compliance_llm_timing or not _compliance_llm_timing.get("total_ms"):
        _compliance_llm_timing = {
            "total_ms": _compliance_dur,
            "load_ms": 0, "pp_ms": 0, "tp_ms": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
        }
    if compliance["verdict"] == "REWRITE" and global_chunks:
        _synth_start2 = time.perf_counter()
        reset_llm_timing()
        final_answer, _ = await synthesize(
            query=query,
            plan=plan,
            subagents=subagents,
            use_case=use_case,
            on_event=on_event,
            compliance_guidance=compliance.get("guidance", "") or
                "; ".join(compliance.get("issues") or []),
            coverage=coverage,
        )
        _synth_llm_timing = get_last_llm_timing()
        _rewrite_dur = round((time.perf_counter() - _synth_start2) * 1000)
        _synth_dur = _rewrite_dur
        phases.append({
            "id": "synthesize_rewrite",
            "label": "Synthese (Korrektur)",
            "ms": _rewrite_dur,
            "llm_ms": (_synth_llm_timing or {}).get("total_ms"),
        })
        # Re-check is intentionally skipped — a single rewrite pass keeps
        # latency bounded. Surface the original verdict so the UI sees that
        # a correction took place.
    elif compliance["verdict"] == "REFUSE":
        final_answer = (
            "Die Antwort konnte nicht belegbar formuliert werden. "
            "Bitte präzisieren Sie Ihre Frage oder laden Sie zusätzliche "
            "Dokumente hoch."
        )

    # ── Plugin layer: execute skill functions the model called ──────────────
    #
    # Before citation mapping on purpose: everything downstream — [n] refs,
    # [BILD:] markers, answer_length — should see the text the user will
    # actually read, not a draft still containing `wetter.aktuell("…")`.
    #
    # Compliance has already run at this point, and that ordering is
    # deliberate too: it judges what the *model* asserted. A value produced by
    # a function is retrieved data, not a claim to be checked for
    # hallucination.
    _skill_fn_t = time.perf_counter()
    skill_function_calls: list[dict] = []
    try:
        final_answer, skill_function_calls = await apply_skill_functions(
            final_answer,
            use_case=use_case,
            session_id=session_id,
            request_id=request_id,
            username=username,
            on_event=on_event,
        )
    except Exception as exc:  # noqa: BLE001
        # The answer is finished; a broken plugin layer must not cost it.
        logger.warning(f"Skill function step failed: {exc}")
    if skill_function_calls:
        phases.append({
            "id": "skill_functions",
            "label": "Skill-Funktionen",
            "ms": round((time.perf_counter() - _skill_fn_t) * 1000),
        })

    _post_t = time.perf_counter()
    citations = map_citations(final_answer, global_chunks)
    # Losing *every* reference while the pool is populated means the
    # synthesizer numbered its citations outside the valid range, not that the
    # sources were fake. Stripping then produces a fully unsourced answer and
    # an empty QUELLEN section — visually indistinguishable from "nothing was
    # found". Log it so this failure mode is diagnosable from the trace.
    if global_chunks and not citations and re.search(r"\[\d+]", final_answer):
        logger.warning(
            "Synthesizer cited only out-of-range chunk numbers over a pool of "
            f"{len(global_chunks)} — the answer will render without sources."
        )
    # Drop any [n] the synthesizer invented beyond the chunk pool so every
    # citation number shown in the answer actually opens a document.
    final_answer = strip_unresolved_refs(final_answer, citations)

    # Bind [BILD: <id>] markers in the final answer to the actual image
    # catalog so the frontend can render them inline. Hallucinated IDs
    # never make it into images_used and are silently dropped client-side.
    available_images = _collect_available_images(global_chunks)
    images_used = select_used_images(final_answer, available_images)

    # Auto-attach fallback: kleine Synth-LLMs setzen den [BILD: <id>]-Marker
    # oft nicht, auch wenn ein Bild zum zitierten Chunk gehört. Wenn der
    # Synthesizer hier nichts markiert hat, aber ein [n]-zitierter Chunk
    # Bilder trägt, hängen wir sie automatisch an — das Frontend rendert
    # sie dann als "Quellbilder" am Ende der Antwort. Das verhindert,
    # dass die visuelle Information (Tabellen-Screenshot, Diagramm) im
    # RAG-Loop verschwindet.
    if not images_used and available_images:
        cited_indices: set[int] = set()
        for m in re.finditer(r"\[(\d+)]", final_answer):
            n = int(m.group(1))
            if 1 <= n <= len(global_chunks):
                cited_indices.add(n - 1)
        cited_image_ids: set[str] = set()
        for idx in cited_indices:
            extra = ((global_chunks[idx].get("metadata") or {}).get("extra")) or {}
            for img in extra.get("images") or []:
                iid = img.get("id") or img.get("image_id")
                if iid:
                    cited_image_ids.add(iid)
        if cited_image_ids:
            by_id = {img["id"]: img for img in available_images}
            images_used = [
                {**by_id[iid], "auto_attached": True}
                for iid in cited_image_ids
                if iid in by_id
            ]

    searched: set[str] = set()
    for sub in subagents:
        searched.update(sub.get("searched_collections") or [])

    # Citation mapping and image attachment only — the similarity enrichment
    # that used to dominate this phase now runs after the answer is sent.
    phases.append({
        "id": "post",
        "label": "Zitate",
        "ms": round((time.perf_counter() - _post_t) * 1000),
    })

    # Flattened compatibility trace so the existing DB column + old UI keep
    # showing *something* sensible. The hierarchical UI uses ``subagents``
    # directly; this list is for fallback display only.
    flat_steps: list[dict] = [{
        "step": 0,
        "thought": plan.get("rationale", ""),
        "action": "MANAGER_PLAN",
        "args": {"subtasks": plan["subtasks"], "merge_strategy": plan["merge_strategy"],
                 "answer_form": plan.get("answer_form")},
        "observation": f"{len(plan['subtasks'])} Sub-Task(s) erzeugt.",
        "chunks": [],
        "duration_ms": _plan_dur,
        "llm_timing": _plan_llm_timing if _plan_llm_timing else None,
    }]
    for sub in subagents:
        for s in sub.get("agent_steps", []):
            flat_steps.append({**s, "subagent_id": sub["subagent_id"],
                               "subagent_role": sub["role"]})
    flat_steps.append({
        "step": len(flat_steps),
        "thought": f"Merge-Strategie: {plan['merge_strategy']}",
        "action": "SYNTHESIZE",
        "args": {"fragment_count": len(subagents),
                 "global_chunk_count": len(global_chunks),
                 "coverage": coverage},
        "observation": final_answer[:300],
        "chunks": global_chunks[:7],
        "duration_ms": _synth_dur,
        "llm_timing": _synth_llm_timing if _synth_llm_timing else None,
    })
    if grounding.get("checked"):
        flat_steps.append({
            "step": len(flat_steps),
            "thought": "Prüfe, ob jede Zahl und jeder Name der Antwort in den Quellen steht",
            "action": "GROUNDING_CHECK",
            "args": {k: grounding[k] for k in ("found", "remaining", "rewritten")},
            "observation": (
                "OK" if not grounding["found"]["numbers"] and not grounding["found"]["names"]
                else "Überarbeitet" if grounding["rewritten"] else "Nicht belegt"
            ),
            "duration_ms": grounding["ms"],
        })
    if skill_function_calls:
        flat_steps.append({
            "step": len(flat_steps),
            "thought": "Ausführbare Skill-Funktionen im Antworttext erkannt",
            "action": "SKILL_FUNCTIONS",
            "args": {"calls": skill_function_calls},
            "observation": ", ".join(
                f"{c['skill']}.{c['function']} → {c['status']}"
                for c in skill_function_calls
            ),
            "chunks": [],
            "duration_ms": round((_post_t - _skill_fn_t) * 1000),
        })
    flat_steps.append({
        "step": len(flat_steps),
        "thought": f"Prüfe Antwort auf Halluzinationen, Citation-Coverage und Policy-Verstöße",
        "action": "COMPLIANCE_CHECK",
        "args": {"verdict": compliance["verdict"],
                 "issues": compliance.get("issues", []),
                 "classified_issues": compliance.get("classified_issues", [])},
        "observation": compliance.get("guidance", "") or "OK" if compliance["verdict"] == "OK" else compliance["verdict"],
        "chunks": [],
        "duration_ms": _compliance_dur,
        "llm_timing": _compliance_llm_timing if _compliance_llm_timing else None,
    })

    sufficient = bool(global_chunks) and any(
        s.get("sufficient") for s in subagents
    )

    # Audit block – the actual model/provider/tuning used and server-side
    # timing, so an exported protocol is genuinely reproducible.
    try:
        _cfg = await resolve_config(use_case)
        _audit = {
            "model": _cfg.llm_model,
            "llm_provider": _cfg.chat_provider,
            "temperature": _cfg.temperature,
            "max_tokens": _cfg.max_tokens,
        }
    except Exception as exc:  # never let auditing break a successful answer
        logger.warning(f"audit config resolve failed: {exc}")
        _audit = {}
    _audit["generated_at"] = datetime.now(timezone.utc).isoformat()
    _audit["processing_ms"] = round((time.perf_counter() - _t0) * 1000)
    _audit["timing"] = timing_report(phases, _audit["processing_ms"])
    _audit["step_count"] = len(flat_steps)
    _audit["subtask_count"] = len(plan["subtasks"])
    _audit["chunk_count"] = len(global_chunks)
    _audit["citation_count"] = len(citations)
    _audit["answer_length"] = len(final_answer)
    _audit["compliance_verdict"] = compliance["verdict"]
    _audit["compliance_issues_count"] = len(compliance.get("issues", []))
    _audit["compliance_classified_issues"] = compliance.get("classified_issues", [])
    _audit["merge_strategy"] = plan["merge_strategy"]
    _audit["sufficient"] = sufficient
    _audit["searched_collections_count"] = len(searched)
    if skill_function_calls:
        _audit["skill_function_calls"] = len(skill_function_calls)
        _audit["skill_function_statuses"] = [
            c["status"] for c in skill_function_calls
        ]

    # Store this Q&A as one chunk in the memory RAG (background, non-blocking).
    # Skip refusals/empty answers so the memory isn't polluted with non-answers.
    if compliance["verdict"] != "REFUSE" and final_answer.strip():
        asyncio.create_task(remember(use_case, query, final_answer))

    result = {
        "answer": final_answer,
        "citations": citations,
        "agent_steps": flat_steps,
        "manager_plan": plan,
        "subagents": list(subagents),
        "global_chunks": global_chunks,
        "compliance": compliance,
        "system_prompt": use_case_prompt,
        "enriched_query": query,
        "use_case_extras": {},
        "audit": _audit,
        "timing": _audit.get("timing"),
        "sufficient": sufficient and compliance["verdict"] != "REFUSE",
        "session_id": session_id,
        "use_case": use_case,
        "searched_collections": sorted(searched),
        "available_images": available_images,
        "images_used": images_used,
        "skill_function_calls": skill_function_calls,
    }
    if on_event:
        await on_event({"type": "final", **result})

    # Diagnostic enrichment runs AFTER the answer is on the wire.
    #
    # similarity_to_rank_1 and answer_similarity are read in exactly one place:
    # the Eval page, which loads them from the database. Nothing in the chat
    # view renders them. Computing them requires embedding every chunk plus the
    # answer, which measured 5.3 s of a 28.4 s request — 19 % of the user's
    # wait for numbers nobody is waiting for. Blocking on it also meant a slow
    # or failing embedding service could delay or sink an answer that was
    # already finished.
    #
    # The chunk dicts are shared by reference with ``result``, so enriching
    # them here still populates what run_manager returns — the caller's DB
    # insert, and with it the Eval page, is unaffected.
    _enrich_start = time.perf_counter()
    try:
        await _enrich_chunk_similarities(list(subagents), global_chunks, final_answer)
    except Exception as exc:  # noqa: BLE001
        # Past the point where this can cost the user anything: log and move on.
        logger.warning(f"Chunk similarity enrichment failed: {exc}")
    _post_ms = round((time.perf_counter() - _enrich_start) * 1000)

    # Recorded separately from ``timing``: that tree measures what the user
    # waited for, and this no longer belongs to it.
    _audit["post_response_ms"] = _post_ms

    if on_event:
        await on_event({
            "type": "chunk_metrics",
            "post_response_ms": _post_ms,
            "by_text_prefix": _chunk_metrics_by_text(subagents, global_chunks),
        })
    return result


def _chunk_metrics_by_text(
    subagents: list[dict], global_chunks: list[dict]
) -> dict[str, dict]:
    """Map chunk-text prefix → the similarity metrics just computed.

    Keyed the way the enrichment itself deduplicates (chunk text), truncated so
    the follow-up event stays small. The client holds the same chunks and can
    patch them without either side tracking positions — which matters because
    the chat export is built from the client's state, not from the database.
    """
    out: dict[str, dict] = {}
    pools = [c for sub in subagents for st in sub.get("agent_steps", [])
             for c in (st.get("chunks") or [])] + list(global_chunks)
    for chunk in pools:
        text = (chunk.get("text") or "").strip()
        if not text or "similarity_to_rank_1" not in chunk:
            continue
        out[text[:200]] = {
            "similarity_to_rank_1": chunk.get("similarity_to_rank_1"),
            "answer_similarity": chunk.get("answer_similarity"),
        }
    return out


__all__ = ["run_manager", "plan_subtasks", "synthesize", "check_compliance"]

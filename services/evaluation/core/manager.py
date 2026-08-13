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
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from config import settings
from core.agent import run_agent
from core.citations import map_citations, strip_unresolved_refs
from core.llm import call_llm
from core.memory_rag import format_memory, recall, remember
from shared.llm import vision_describe
from shared.llm.pipeline import get_last_llm_timing, reset_llm_timing
from shared.usecase_config import resolve_config
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
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)


# ── Decomposer ──────────────────────────────────────────────────────────────

_DECOMPOSER_SYSTEM = """Du bist der Manager-Agent eines dokumenten-fokussierten RAG-Systems.

Deine einzige Aufgabe: die Nutzer-Anfrage in 1-3 sinnvolle Sub-Tasks
zerlegen, die parallel von Spezialisten bearbeitet werden.

Verfügbare Rollen:
- "facts":     konkrete Werte, Zahlen, Namen, Eigenschaften, kurze Fakten
- "procedure": Abläufe, Anleitungen, Schritt-für-Schritt, Workflows
- "context":   Hintergrund, Definitionen, Begriffsklärungen, Rahmen

Merge-Strategien:
- "complementary": Antworten ergänzen sich (Standard, z.B. Fakt + Hintergrund)
- "comparative":   Antworten werden gegenübergestellt (z.B. A vs. B)
- "fallback":      die Sub-Tasks beantworten dieselbe Frage auf zwei Wegen

REGELN:
- Bei einer einfachen Faktenfrage reicht EIN Sub-Task mit "facts".
- Bei Vergleichen pro Entität ein eigener Sub-Task (meist "facts").
- "procedure" nur, wenn explizit nach Ablauf/Anleitung gefragt ist.
- "context" nur, wenn Hintergrund/Definitionen wirklich helfen würden.
- NIE mehr als 3 Sub-Tasks. Lieber einen weglassen als zwei ähnliche.
- Jede sub_query muss eigenständig durchsuchbar sein (keine Pronomen,
  keine Bezüge auf andere Sub-Tasks).

Antworte AUSSCHLIESSLICH mit gültigem JSON in genau diesem Schema:

{
  "rationale": "kurz auf Deutsch, warum du so zerlegst",
  "merge_strategy": "complementary" | "comparative" | "fallback",
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
        "subtasks": [
            {
                "role": DEFAULT_ROLE,
                "sub_query": query,
                "focus": "Direkte Beantwortung der Originalfrage.",
            }
        ],
    }


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
        focus = (st.get("focus") or "").strip()
        cleaned.append({"role": role, "sub_query": sub_query, "focus": focus})

    if not cleaned:
        return _fallback_plan(fallback_query)

    merge = raw.get("merge_strategy")
    if merge not in ("complementary", "comparative", "fallback"):
        merge = "complementary"

    return {
        "rationale": (raw.get("rationale") or "").strip()[:500],
        "merge_strategy": merge,
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
) -> dict:
    """Ask the manager LLM to decompose the query.

    Returns a normalised plan; falls back to a single ``facts`` sub-task on
    LLM/parse failure. Never raises. When ``memory`` is provided (typically for
    a vague/incomplete query) it is offered to the decomposer so it can turn an
    underspecified request into concrete, well-scoped sub-queries.
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

    messages = [
        {"role": "system", "content": _DECOMPOSER_SYSTEM},
        {"role": "user", "content": "\n".join(user_msg_parts)},
    ]

    try:
        raw_response = await call_llm(messages, use_case=use_case)
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
    return _normalise_plan(parsed, query)


# ── Sub-agent orchestration ─────────────────────────────────────────────────


def _build_sub_system_prompt(base_prompt: str, role: RoleConfig, focus: str) -> str:
    """Compose the use-case prompt with the role-specific suffix + focus hint."""
    focus_block = f"\nFOKUS DIESES SUB-TASKS: {focus}\n" if focus else ""
    return f"{base_prompt}\n{role.prompt_suffix}{focus_block}"


def _wrap_event(
    on_event: StepCallback, *, sub_id: str, role: str
) -> StepCallback:
    """Wrap an ``on_event`` callback so sub-agent events are tagged.

    Returns ``None`` if the caller didn't supply a callback — keeps the
    ``run_agent`` signature happy.
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
) -> dict:
    """Run a single sub-agent. Wraps ``run_agent`` with the role config."""
    _t0 = time.perf_counter()
    sub_actions = filter_actions(role, available_actions)
    sub_prompt = _build_sub_system_prompt(use_case_prompt, role, focus)

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
            on_event=_wrap_event(on_event, sub_id=sub_id, role=role.name),
        )
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
1. Citation-Coverage: Wird jede Tatsachenbehauptung mit [n] belegt?
2. Quellenechtheit: Belegen die referenzierten Chunks die Aussage tatsächlich?
3. Halluzination: Enthält die Antwort Fakten, die in keinem Chunk stehen?
4. Use-Case-Policy: Wenn der System-Prompt Regeln nennt, werden sie eingehalten?

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
- Jede Tatsachenbehauptung muss mit [n] belegt sein, wobei n die globale
  Chunk-Nummer ist (nicht die lokale Nummerierung der Spezialisten).
- Widersprüche zwischen Quellen NICHT überdecken — markiere sie
  ("Quelle [3] gibt X an, Quelle [5] dagegen Y").
- Wenn ein Teil der Originalfrage durch keinen Chunk gedeckt ist, sage
  das wörtlich.
- KEINE eigenen Ergänzungen, kein Allgemeinwissen, keine Trainings-Daten.
- Antworte ohne Vor- oder Nachspann, ohne Meta-Kommentare.

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
    return [seen[k] for k in order]


def _format_global_chunks(chunks: list[dict]) -> str:
    """Render the global chunk pool for the synthesizer prompt."""
    if not chunks:
        return "(Keine Chunks – die Spezialisten haben nichts gefunden.)"
    lines = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("metadata") or {}
        text = (c.get("text") or "")[:400]
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
                "alt_text": (img.get("alt_text") or "")[:300],
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


def _format_fragments(subagents: list[dict]) -> str:
    """Render the sub-agent answer fragments for the synthesizer prompt."""
    lines = []
    for sub in subagents:
        header = f"--- {sub['role_label']} (Sub-Query: {sub['sub_query']}) ---"
        body = (sub.get("answer") or "(leer)").strip()
        if sub.get("error"):
            body = f"(Fehler: {sub['error']})"
        lines.append(f"{header}\n{body}")
    return "\n\n".join(lines)


async def synthesize(
    *,
    query: str,
    plan: dict,
    subagents: list[dict],
    use_case: str,
    on_event: StepCallback,
    compliance_guidance: str = "",
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
    user = (
        f"ORIGINALFRAGE:\n{query}\n\n"
        f"PLAN-RATIONALE (Manager):\n{plan.get('rationale', '')}\n\n"
        f"ANTWORT-FRAGMENTE DER SPEZIALISTEN:\n{_format_fragments(subagents)}\n\n"
        f"GLOBAL_CHUNK_POOL (für [n]-Verweise):\n{_format_global_chunks(global_chunks)}\n\n"
        f"AVAILABLE_IMAGES (für [BILD: <id>]-Verweise):\n"
        f"{_format_available_images(available_images)}\n"
        f"{guidance_block}\n"
        "Formuliere jetzt die finale Antwort."
    )

    try:
        final = await call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            use_case=use_case,
        )
    except Exception as exc:
        logger.warning(f"Synthesizer LLM failed: {exc} — falling back to concatenation")
        final = _concat_fragments_fallback(subagents)

    final = _clean_synthesized(final)
    if on_event:
        await on_event({"type": "synthesizer", "phase": "done",
                        "answer_length": len(final)})
    return final, global_chunks


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


async def check_compliance(
    *,
    answer: str,
    global_chunks: list[dict],
    use_case_prompt: str,
    use_case: str,
    on_event: StepCallback,
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

    user = (
        f"USE-CASE-POLICY (aus dem System-Prompt):\n{use_case_prompt}\n\n"
        f"GLOBAL_CHUNK_POOL:\n{_format_global_chunks(global_chunks)}\n\n"
        f"FINALE ANTWORT:\n{answer}\n\n"
        "Beurteile die Antwort."
    )
    try:
        raw = await call_llm(
            [{"role": "system", "content": _COMPLIANCE_SYSTEM},
             {"role": "user", "content": user}],
            use_case=use_case,
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

    # Classify issues via NLI if any
    # Classify issues via NLI if any (with fallback — don't crash compliance on NLI failure)
    classified_issues = await _classify_compliance_issues(issues) if issues else []

    result = {"verdict": verdict, "issues": issues, "classified_issues": classified_issues, "guidance": guidance}
    if on_event:
        await on_event({"type": "compliance", "phase": "done", **result})
    return result


_SYNTH_CLEANUP_RE = re.compile(
    r"^\s*(?:FINAL_ANSWER|ANTWORT)\s*[:(]\s*",
    re.IGNORECASE,
)


def _clean_synthesized(text: str) -> str:
    """Trim synthesizer output of common artefacts (leading labels, fences)."""
    text = text.strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = _SYNTH_CLEANUP_RE.sub("", text)
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
    _best_score = -1.0
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

    _plan_t = time.perf_counter()
    plan = await plan_subtasks(
        query, use_case=use_case, history=history,
        image_context=image_context, memory=memory,
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
    for i, st in enumerate(plan["subtasks"], 1):
        role = get_role(st["role"])
        sub_id = f"sub-{i}-{role.name}"
        subagent_coros.append(
            _run_one_subagent(
                sub_id=sub_id,
                sub_query=st["sub_query"],
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
            )
        )
    _subs_t = time.perf_counter()
    subagents = await asyncio.gather(*subagent_coros)
    _subs_dur = round((time.perf_counter() - _subs_t) * 1000)
    phases.append({
        "id": "subagents",
        "label": "Sub-Agents (parallel)",
        "ms": _subs_dur,
        "children": subagent_phase_children(list(subagents)),
    })

    # Synthesize
    _synth_start = time.perf_counter()
    reset_llm_timing()  # guard against synthesize skipping the LLM call
    final_answer, global_chunks = await synthesize(
        query=query,
        plan=plan,
        subagents=list(subagents),
        use_case=use_case,
        on_event=on_event,
    )
    _synth_llm_timing = get_last_llm_timing()
    _synth_dur = round((time.perf_counter() - _synth_start) * 1000)
    phases.append({
        "id": "synthesize",
        "label": "Synthese",
        "ms": _synth_dur,
        "llm_ms": (_synth_llm_timing or {}).get("total_ms"),
    })

    # Compliance check + optional one-shot rewrite
    _compliance_start = time.perf_counter()
    reset_llm_timing()  # guard against compliance skipping the LLM call
    compliance = await check_compliance(
        answer=final_answer,
        global_chunks=global_chunks,
        use_case_prompt=use_case_prompt,
        use_case=use_case,
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
            subagents=list(subagents),
            use_case=use_case,
            on_event=on_event,
            compliance_guidance=compliance.get("guidance", "") or
                "; ".join(compliance.get("issues") or []),
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

    _post_t = time.perf_counter()
    citations = map_citations(final_answer, global_chunks)
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

    # Enrich chunks with similarity metrics (uses embedding service)
    await _enrich_chunk_similarities(list(subagents), global_chunks, final_answer)
    phases.append({
        "id": "post",
        "label": "Zitate & Anreicherung",
        "ms": round((time.perf_counter() - _post_t) * 1000),
    })

    # Flattened compatibility trace so the existing DB column + old UI keep
    # showing *something* sensible. The hierarchical UI uses ``subagents``
    # directly; this list is for fallback display only.
    flat_steps: list[dict] = [{
        "step": 0,
        "thought": plan.get("rationale", ""),
        "action": "MANAGER_PLAN",
        "args": {"subtasks": plan["subtasks"], "merge_strategy": plan["merge_strategy"]},
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
                 "global_chunk_count": len(global_chunks)},
        "observation": final_answer[:300],
        "chunks": global_chunks[:7],
        "duration_ms": _synth_dur,
        "llm_timing": _synth_llm_timing if _synth_llm_timing else None,
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
    }
    if on_event:
        await on_event({"type": "final", **result})
    return result


__all__ = ["run_manager", "plan_subtasks", "synthesize", "check_compliance"]

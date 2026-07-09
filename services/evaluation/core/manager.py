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
from shared.usecase_config import resolve_config
from core.roles import (
    DEFAULT_ROLE,
    VALID_ROLES,
    RoleConfig,
    filter_actions,
    get_role,
)

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


_BILD_MARKER_RE = re.compile(r"\[BILD:\s*(img_[A-Za-z0-9_]+)\s*\]")


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
        return {"verdict": "OK", "issues": [], "guidance": ""}

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
        return {"verdict": "OK", "issues": [], "guidance": ""}

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        if on_event:
            await on_event({"type": "compliance", "phase": "done",
                            "verdict": "OK", "issues": [], "guidance": ""})
        return {"verdict": "OK", "issues": [], "guidance": ""}

    verdict = parsed.get("verdict") if parsed.get("verdict") in ("OK", "REWRITE", "REFUSE") else "OK"
    issues = parsed.get("issues") if isinstance(parsed.get("issues"), list) else []
    guidance = parsed.get("guidance") if isinstance(parsed.get("guidance"), str) else ""

    result = {"verdict": verdict, "issues": issues, "guidance": guidance}
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
    if on_event:
        await on_event({"type": "started", "stage": "manager"})

    # If the user attached image(s), describe them with the vision model first
    # so the content is available as text to the planner and every sub-agent
    # (works regardless of whether the chat model is multimodal).
    image_context = ""
    if images:
        if on_event:
            await on_event({"type": "image_analysis", "count": len(images)})
        image_context = await describe_uploaded_images(images, use_case=use_case)

    # Vague/incomplete query → recall the most relevant past Q&A from the
    # memory RAG so the manager can plan concrete sub-tasks instead of guessing.
    # Gated on vagueness so clear queries pay no extra embed+search cost.
    memory = ""
    if _is_vague_query(query, history):
        mems = await recall(use_case, query)
        if mems:
            if on_event:
                await on_event({"type": "memory_recall", "count": len(mems)})
            memory = format_memory(mems)

    plan = await plan_subtasks(
        query, use_case=use_case, history=history,
        image_context=image_context, memory=memory,
    )
    if on_event:
        await on_event({"type": "manager_plan", **plan})

    # Fan-out — run sub-agents in parallel. Each gets a stable ``sub_id``
    # used by the frontend to group their events.
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
    subagents = await asyncio.gather(*subagent_coros)

    # Synthesize
    final_answer, global_chunks = await synthesize(
        query=query,
        plan=plan,
        subagents=list(subagents),
        use_case=use_case,
        on_event=on_event,
    )

    # Compliance check + optional one-shot rewrite
    compliance = await check_compliance(
        answer=final_answer,
        global_chunks=global_chunks,
        use_case_prompt=use_case_prompt,
        use_case=use_case,
        on_event=on_event,
    )
    if compliance["verdict"] == "REWRITE" and global_chunks:
        final_answer, _ = await synthesize(
            query=query,
            plan=plan,
            subagents=list(subagents),
            use_case=use_case,
            on_event=on_event,
            compliance_guidance=compliance.get("guidance", "") or
                "; ".join(compliance.get("issues") or []),
        )
        # Re-check is intentionally skipped — a single rewrite pass keeps
        # latency bounded. Surface the original verdict so the UI sees that
        # a correction took place.
    elif compliance["verdict"] == "REFUSE":
        final_answer = (
            "Die Antwort konnte nicht belegbar formuliert werden. "
            "Bitte präzisieren Sie Ihre Frage oder laden Sie zusätzliche "
            "Dokumente hoch."
        )

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

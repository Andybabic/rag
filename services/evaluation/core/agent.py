"""ReAct agent core – Reason → Act → Observe loop."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

StepCallback = Optional[Callable[[dict], Awaitable[None]]]

from core.actions import (
    action_clarify,
    action_lookup_sources,
    action_recall_memory,
    action_refine_query,
    action_search,
    action_search_cnc,
)
from core.citations import map_citations, strip_unresolved_refs
from core.llm import TOKENS_AGENT_STEP, call_llm
from core.memory import read_memory, write_memory
from core.prompts import ACTION_SIGNATURES
from core.use_cases import get_react_suffix
from shared.llm.pipeline import get_last_llm_timing
from shared.llm.thinking import strip_thinking
from shared.usecase_config import list_skills

_ACTION_NAME_RE = re.compile(r"ACTION:\s*(\w+)\(", re.DOTALL)

_THOUGHT_RE = re.compile(r"THOUGHT:\s*(.*?)(?=ACTION:|$)", re.DOTALL)
_CLEANUP_RE = re.compile(
    r"(?:^|\n)\s*(?:THOUGHT|ACTION|OBSERVATION):.*",
    re.DOTALL,
)

# Detects answers that really mean "the documents don't contain this" so we
# can (a) report sufficient=False instead of a confident-looking failure and
# (b) force one broadened retry before giving up.
_NO_INFO_RE = re.compile(
    r"(keine\s+(?:passenden\s+|spezifischen\s+|konkreten\s+)?"
    r"(?:information|informationen|angabe|angaben|hinweise|dokumente)"
    r"|nicht\s+(?:in\s+den\s+|enthalten|dokumentiert|genannt|beschrieben)"
    r"|enthalten\s+die\dokumente\s+keine"
    r"|liegen\s+keine\s+.*vor"
    r"|keine\s+\S+(?:\s+\S+){0,4}?\s+vorliegen"
    r"|(?:handlungsempfehlung|empfehlung|aussage|antwort)\s+nicht\s+möglich"
    r"|wurden\s+keine\s+.*gefunden)",
    re.IGNORECASE,
)


def _is_no_info_answer(text: str) -> bool:
    """True if the answer essentially says 'not found in the documents'."""
    t = (text or "").strip()
    if not t:
        return True
    return bool(_NO_INFO_RE.search(t))


# An "answer" that is really an action call the model failed to emit as one.
# The ReAct parser falls back to FINAL_ANSWER when it cannot find a valid
# action, so a malformed SEARCH does not surface as a parse error — it becomes
# the specialist's answer, is marked sufficient, and travels to the synthesizer
# as if the specialist had contributed. Observed verbatim as a whole sub-agent
# answer: 'SEARCH({"query": "…", "collection": "OBSERVATION", "filters": {}'.
#
# Anchored, and requires the opening brace of the argument object: a German
# answer legitimately opening with an acronym and a parenthesis ("USTP (Use
# Case) …") must not be discarded as a failed step.
_ACTION_CALL_ANSWER_RE = re.compile(r"^\s*(?:[A-Z][A-Z_]{2,}\s*\(\s*\{|\{\s*\"action\"\s*:)")


def _is_action_call_answer(text: str) -> bool:
    """True if *text* is an action invocation rather than a prose answer."""
    return bool(_ACTION_CALL_ANSWER_RE.match(text or ""))


def _broaden_query(original: str, narrowed: str) -> str:
    """Build one broader retry query: union of the user's full question and
    the model's (often narrowed) search terms, deduplicated word-wise. No
    LLM call – deterministic and latency-free."""
    seen: set[str] = set()
    words: list[str] = []
    for w in f"{original} {narrowed}".split():
        key = w.lower().strip(".,;:!?\"'()[]„""")
        if key and key not in seen:
            seen.add(key)
            words.append(w)
    return " ".join(words).strip() or original


def _extract_json_arg(text: str, start: int) -> str | None:
    """Extract a balanced JSON object starting at position *start* in *text*.

    Handles nested braces correctly so that multi-line / nested JSON like
    ``{"filters": {"key": [1,2]}}`` is captured in full.
    """
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _format_skills_block(skills: list) -> str:
    """Format enabled skills as a "Zusätzliche Regeln" section.

    Empty string when no skills are enabled — keeps the prompt clean.
    """
    if not skills:
        return ""
    lines = ["# Zusätzliche Regeln", ""]
    lines.append(
        "Beachte die folgenden Regeln bei jeder Anfrage. Wenn eine Regel "
        "greift, passe deinen Workflow entsprechend an (erweitere Schritte, "
        "brich ab, oder ändere die Antwort)."
    )
    lines.append("")
    for skill in skills:
        lines.append(f"## {skill.name} — {skill.overview}")
        lines.append(skill.detailed_task.strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n\n"


async def _build_system_prompt(
    use_case: str,
    use_case_prompt: str,
    available_actions: list[str],
) -> str:
    """Build the full system prompt with action descriptions and active skills.

    Pulls the global ReAct suffix and the enabled per-usecase skills from the
    resolver so admins can edit both via the UI; falls back gracefully.
    """
    action_lines = []
    for action in available_actions:
        if action in ACTION_SIGNATURES:
            action_lines.append(f"- {ACTION_SIGNATURES[action]}")

    suffix = await get_react_suffix()
    skills = await list_skills(use_case, only_enabled=True)
    skills_block = _format_skills_block(skills)
    return (
        f"{use_case_prompt}\n\n"
        f"Verfügbare Actions (immer als JSON-Argument):\n"
        f"{chr(10).join(action_lines)}\n\n"
        f"{skills_block}"
        f"{suffix}"
    )


def _parse_action(llm_response: str) -> tuple[str, str, dict]:
    """Parse THOUGHT and ACTION from LLM response.

    Returns (thought, action_name, action_args).
    Falls back to FINAL_ANSWER with the entire response if no action found.
    """
    llm_response = strip_thinking(llm_response or "")
    thought_match = _THOUGHT_RE.search(llm_response)
    thought = thought_match.group(1).strip() if thought_match else ""

    action_match = _ACTION_NAME_RE.search(llm_response)
    if action_match:
        action_name = action_match.group(1)
        # Find the opening '{' after 'ACTION: NAME('
        json_start = llm_response.find("{", action_match.end())
        # Extract balanced JSON (handles nested braces / multi-line)
        if json_start == -1:
            return thought, action_name, {}
        raw_json = _extract_json_arg(llm_response, json_start)
        if raw_json:
            try:
                action_args = json.loads(raw_json)
            except json.JSONDecodeError:
                action_args = {}
            return thought, action_name, action_args
        return thought, action_name, {}

    # Fallback: treat entire response as FINAL_ANSWER
    return thought or llm_response, "FINAL_ANSWER", {"answer": llm_response, "extras": {}}


async def _execute_action(
    action_name: str,
    action_args: dict,
    *,
    session_id: str,
    use_case: str,
    collection: str = "",
    filters: dict | None = None,
) -> str | dict:
    """Execute an action and return the observation string (or dict with chunks).

    Wrapped with an OpenInference tool span so Phoenix tracks tool usage.
    """
    try:
        from shared.phoenix import get_tracer
        tracer = get_tracer("agent")
    except Exception:
        tracer = None

    if tracer is None:
        return await _execute_action_inner(
            action_name, action_args,
            session_id=session_id, use_case=use_case,
            collection=collection, filters=filters,
        )

    from openinference.semconv.trace import SpanAttributes, OpenInferenceSpanKindValues

    with tracer.start_as_current_span(f"tool.{action_name.lower()}") as span:
        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.TOOL.value)
        span.set_attribute(SpanAttributes.TOOL_NAME, action_name)
        span.set_attribute(SpanAttributes.TOOL_PARAMETERS, str(action_args))

        try:
            result = await _execute_action_inner(
                action_name, action_args,
                session_id=session_id, use_case=use_case,
                collection=collection, filters=filters,
            )
            if isinstance(result, dict):
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, result.get("observation", str(result)))
            else:
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, str(result)[:500])
            return result
        except Exception as exc:
            span.record_exception(exc)
            raise


async def _execute_action_inner(
    action_name: str,
    action_args: dict,
    *,
    session_id: str,
    use_case: str,
    collection: str = "",
    filters: dict | None = None,
) -> str | dict:
    """Execute an action and return the observation string (or dict with chunks)."""
    if action_name == "SEARCH":
        return await action_search(action_args, use_case=use_case)
    if action_name == "SEARCH_CNC":
        return await action_search_cnc(action_args, use_case=use_case)
    if action_name == "REFINE_QUERY":
        return await action_refine_query(
            action_args, use_case=use_case, collection=collection, filters=filters
        )
    if action_name == "CLARIFY":
        return await action_clarify(action_args)
    if action_name == "RECALL_MEMORY":
        return await action_recall_memory(action_args, use_case)
    if action_name == "LOOKUP_SOURCES":
        return await action_lookup_sources(action_args, use_case)
    if action_name == "FINAL_ANSWER":
        return action_args.get("answer", "")
    return f"Unbekannte Action: {action_name}"


async def _update_memory(use_case: str, query: str, answer: str) -> None:
    """Update use-case memory with new insights (non-blocking)."""
    existing = await read_memory(use_case)
    new_entry = f"Q: {query}\nA: {answer[:500]}"
    if existing:
        updated = f"{existing}\n---\n{new_entry}"
    else:
        updated = new_entry
    await write_memory(use_case, updated)


def _enrich_query(query: str, history: list[dict] | None) -> str:
    """Enrich short follow-up messages with conversation context.

    If the user sends a short reply like "ja", "die Filteranlage", or
    "Modell 2699", this combines it with the previous questions/answers
    to form a meaningful search query.
    """
    if not history or len(query.split()) > 8:
        # Query is already detailed enough
        return query

    # Collect the original user question(s) from history
    user_questions = [
        m["content"] for m in history if m["role"] == "user"
    ]
    if not user_questions:
        return query

    # Build a combined context: original question + current follow-up
    original = user_questions[0]
    context_parts = [original]

    # Add any subsequent user messages (refinements)
    for uq in user_questions[1:]:
        if len(uq.split()) <= 8:
            context_parts.append(uq)
        else:
            context_parts.append(uq)

    context_parts.append(query)

    return (
        f"Kontext aus dem Gespräch – der Benutzer hat folgende Nachrichten gesendet:\n"
        + "\n".join(f"- {p}" for p in context_parts)
        + "\n\nBeantworte die Frage basierend auf dem gesamten Kontext."
    )


_CLOSING_DEMAND = (
    "Der Schrittvorrat ist aufgebraucht. Formuliere JETZT die Antwort auf die "
    "ursprüngliche Frage, ausschliesslich aus den OBSERVATION-Chunks oben.\n"
    "- Belege jede Aussage mit der Chunk-Nummer [n] aus den Beobachtungen.\n"
    "- Bildbeschreibungen ([BILD …]) sind dabei eine gültige Quelle.\n"
    "- Decken die Chunks die Frage nicht ab, sage genau das.\n"
    "Antworte als reiner Fliesstext — KEIN THOUGHT, KEIN ACTION, kein JSON."
)


async def _closing_answer(
    messages: list[dict], *, use_case: str
) -> tuple[str, bool]:
    """Force an answer out of a loop that ran out of steps without one.

    Returns ``(answer, sufficient)``; ``("", False)`` when the call fails, so
    the caller falls through to its raw-chunk fallback. That fallback is what
    this exists to avoid: it dumps observations under a local [1..n] numbering
    which, as a manager fragment, is unusable to the synthesizer.
    """
    try:
        raw = await call_llm(
            messages + [{"role": "user", "content": _CLOSING_DEMAND}],
            use_case=use_case,
            max_tokens=TOKENS_AGENT_STEP,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Closing answer call failed: {exc}")
        return "", False

    # The model may still wrap the reply in the ReAct format out of habit, so
    # unwrap a FINAL_ANSWER argument when there is one. Note _parse_action
    # falls back to FINAL_ANSWER carrying the *entire* response when it finds
    # no ACTION, so this branch also covers plain prose. Leftover framework
    # artefacts are stripped by run_agent's cleanup on the way out — kept in
    # one place rather than duplicated here.
    _, action_name, action_args = _parse_action(raw)
    if action_name == "FINAL_ANSWER":
        raw = str(action_args.get("answer") or raw)

    text = raw.strip()
    return (text, True) if text else ("", False)


async def run_agent(
    query: str,
    *,
    use_case: str,
    session_id: str,
    system_prompt: str,
    available_actions: list[str],
    collection: str = "",
    filters: dict | None = None,
    max_steps: int = 5,
    history: list[dict] | None = None,
    images: list[str] | None = None,
    image_context: str = "",
    persist_memory: bool = True,
    on_event: StepCallback = None,
) -> dict:
    """Run the ReAct agent loop.

    If ``on_event`` is provided, it is awaited for each progress event – this
    lets a streaming endpoint push live updates to the client without changing
    the function's return shape. Events:
      - {"type": "started", ...}
      - {"type": "thinking", "step": N}
      - {"type": "step", "step": N, ...full step dict...}
      - {"type": "final", "answer": ...}
    """
    full_system = await _build_system_prompt(use_case, system_prompt, available_actions)
    if on_event:
        await on_event({"type": "started", "max_steps": max_steps})

    # Enrich short follow-up messages with conversation context
    # e.g. "ja" or "die Filteranlage" → combine with previous question
    enriched_query = _enrich_query(query, history)

    messages: list[dict] = [
        {"role": "system", "content": full_system},
    ]

    # Add conversation history (previous Q&A pairs as context)
    if history:
        # Keep last 6 messages (3 pairs) to avoid context overflow
        for msg in history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    # When an image was uploaded, prepend its (vision-generated) description as
    # context so the answer can be grounded in the image even if the chat model
    # itself isn't multimodal. ``enriched_query`` stays clean so it can still
    # seed document searches without the description leaking into them.
    user_content = enriched_query
    if image_context:
        user_content = (
            "Der Nutzer hat ein Bild hochgeladen. Automatisch erkannter "
            f"Bildinhalt:\n{image_context}\n\n"
            "Beantworte die Frage in erster Linie anhand des Bildes. Suche nur "
            "dann in den Dokumenten, wenn das für die Frage zusätzlich relevant "
            f"ist.\n\nFrage: {enriched_query}"
        )

    # Attach the raw image(s) to the first user turn too, so a vision-capable
    # chat model can also look at them directly. They stay in the message list
    # across ReAct iterations, so the model keeps seeing them.
    user_msg: dict = {"role": "user", "content": user_content}
    if images:
        user_msg["images"] = images
    messages.append(user_msg)

    # An uploaded image means the user may just want to ask about the image;
    # in that case RAG is optional, so we must not force a document search.
    has_image = bool(images) or bool(image_context)

    steps: list[dict] = []
    answer = ""
    use_case_extras: dict = {}
    chunks_for_citations: list[dict] = []
    searched_collections: set[str] = set()
    sufficient = False

    has_searched = False
    broadened_retry_done = False
    last_search_query = enriched_query

    for step_num in range(1, max_steps + 1):
        if on_event:
            await on_event({"type": "thinking", "step": step_num})

        # Spending the last step on another SEARCH leaves the loop with no
        # answer, and the fallback below then dumps raw chunks under its own
        # local [1..n] numbering. As a manager fragment that dump is worse than
        # useless: the synthesizer cannot map those numbers onto the global
        # pool and responds by dropping citations from the final answer
        # altogether. Demand the answer while the model can still write one.
        if step_num == max_steps and has_searched:
            messages.append({
                "role": "user",
                "content": (
                    "LETZTER SCHRITT: Weitere Suchen sind nicht mehr möglich. "
                    "Antworte JETZT mit FINAL_ANSWER auf Basis der bisherigen "
                    "OBSERVATION-Chunks. Belege jede Aussage mit der "
                    "Chunk-Nummer [n] aus den Beobachtungen. Wenn die Chunks "
                    "die Frage nicht abdecken, sage das ausdrücklich — auch "
                    "das ist eine gültige FINAL_ANSWER."
                ),
            })

        # 1. Call LLM (use_case threads provider/model overrides through resolver)
        _step_start = time.perf_counter()
        llm_response = await call_llm(
            messages, use_case=use_case, max_tokens=TOKENS_AGENT_STEP
        )
        _llm_timing_data = get_last_llm_timing()
        _llm_ms = round((time.perf_counter() - _step_start) * 1000)
        thought, action_name, action_args = _parse_action(llm_response)

        # Hard guard: small models (e.g. qwen3:8b) sometimes skip SEARCH on
        # short follow-up questions and go straight to FINAL_ANSWER from the
        # conversation history. Force a SEARCH first so RAG actually runs.
        # Skipped when an image was uploaded — there RAG is optional and the
        # user may just be asking about the image itself.
        if not has_searched and not has_image and action_name in ("FINAL_ANSWER", "CLARIFY"):
            action_name = "SEARCH"
            action_args = {"query": enriched_query}
            thought = (
                (thought + "\n" if thought else "")
                + "[Auto-Korrektur: SEARCH erzwungen, bevor eine Antwort gegeben wird.]"
            )

        # Second guard: don't accept a "not in the documents" answer on the
        # first try. A narrowed query often misses content that a broader
        # phrasing would surface. Force exactly one broadened REFINE_QUERY.
        elif (
            action_name == "FINAL_ANSWER"
            and not broadened_retry_done
            and not has_image
            and _is_no_info_answer(action_args.get("answer", ""))
        ):
            broadened_retry_done = True
            action_name = "REFINE_QUERY"
            action_args = {
                "query": _broaden_query(enriched_query, last_search_query),
                "reason": "Erste Suche ohne belastbaren Treffer – breitere Formulierung",
            }
            thought = (
                (thought + "\n" if thought else "")
                + "[Auto-Korrektur: breitere Suche erzwungen, bevor 'keine "
                "Information' zurückgegeben wird.]"
            )

        if on_event:
            await on_event({
                "type": "action",
                "step": step_num,
                "action": action_name,
                "thought": thought,
                "args": action_args,
            })

        # Force collection/filters for SEARCH – LLM must not override these
        if action_name == "SEARCH":
            action_args["collection"] = collection
            action_args["filters"] = filters or action_args.get("filters", {})
            if "query" not in action_args:
                action_args["query"] = enriched_query
            has_searched = True
            last_search_query = action_args.get("query") or last_search_query
        elif action_name == "REFINE_QUERY":
            has_searched = True
            last_search_query = action_args.get("query") or last_search_query
        elif action_name == "SEARCH_CNC":
            # SEARCH_CNC ist eine vollwertige Retrieval-Action. Ohne dies
            # erzwingt der Guardrail unten ein generisches SEARCH und holt
            # damit irrelevante Treffer (Bohrer/Anbohrer) zurück, die eine
            # saubere CNC-Werkzeugempfehlung wieder verwässern.
            has_searched = True

        # 2. Execute action
        _action_start = time.perf_counter()
        result = await _execute_action(
            action_name,
            action_args,
            session_id=session_id,
            use_case=use_case,
            collection=collection,
            filters=filters,
        )

        # Handle structured results (SEARCH / REFINE_QUERY return dict with chunks)
        step_chunks: list[dict] = []
        if isinstance(result, dict):
            observation = result.get("observation", "")
            step_chunks = result.get("chunks", [])
            chunks_for_citations.extend(step_chunks)
            searched_collections.update(result.get("searched_collections", []))
            if action_name == "REFINE_QUERY":
                has_searched = True
        else:
            observation = result

        # 3. Log step – keep the full trace so the UI can show what the LLM
        #    saw (raw response) and which chunks it received for each SEARCH.
        _action_ms = round((time.perf_counter() - _action_start) * 1000)
        _step_dur = round((time.perf_counter() - _step_start) * 1000)
        step_dict = {
            "step": step_num,
            "thought": thought,
            "action": action_name,
            "args": action_args,
            "observation": observation,
            "llm_response": llm_response,
            "chunks": step_chunks,
            "duration_ms": _step_dur,
            "llm_ms": _llm_ms,
            "action_ms": _action_ms,
            "llm_timing": _llm_timing_data if _llm_timing_data else None,
            "embed_ms": result.get("embed_ms") if isinstance(result, dict) else None,
            "search_ms": result.get("search_ms") if isinstance(result, dict) else None,
            "rerank_ms": result.get("rerank_ms") if isinstance(result, dict) else None,
        }
        steps.append(step_dict)
        if on_event:
            await on_event({"type": "step", **step_dict})

        # 4. Check for terminal actions
        if action_name == "FINAL_ANSWER":
            answer = action_args.get("answer", observation)
            use_case_extras = action_args.get("extras", {})
            if _is_action_call_answer(answer):
                # Drop it rather than pass it on. Emptying the answer hands the
                # step to the closing call below, which sees the same
                # observations and can do nothing but answer — one generation
                # instead of a fragment that is neither an answer nor an error.
                logger.warning(
                    "Sub-agent returned an action call as its FINAL_ANSWER "
                    "(%.80s…) — discarding and forcing a closing answer.",
                    answer.replace("\n", " "),
                )
                answer = ""
            else:
                # Honest signal: a "not in the documents" answer is NOT a
                # sufficient RAG result. This propagates to the manager,
                # synthesizer, compliance and the UI/audit instead of looking
                # like a confident answer.
                sufficient = not _is_no_info_answer(answer)
            break

        if action_name == "CLARIFY":
            # Return the clarification question as the answer
            clarify_text = observation.replace("CLARIFICATION_NEEDED: ", "")
            answer = clarify_text
            sufficient = False
            break

        # 5. Add to message history for next iteration
        messages.append({"role": "assistant", "content": llm_response})
        messages.append({"role": "user", "content": f"OBSERVATION: {observation}"})

    # The step budget is gone and the model never answered. Asking for the
    # answer inside the loop is only a prompt, and this model ignores it often
    # enough that we cannot rely on it — so spend one closing call that can do
    # nothing *but* answer. It sees every observation already collected, so it
    # costs one generation and no further retrieval.
    if not answer and chunks_for_citations:
        answer, sufficient = await _closing_answer(messages, use_case=use_case)

    # If max_steps reached without FINAL_ANSWER – try to use whatever we found
    if not answer:
        if chunks_for_citations:
            # We have search results but the LLM never gave a FINAL_ANSWER
            # Build a response from the collected chunks
            lines = ["Basierend auf den gefundenen Dokumenten:\n"]
            for i, c in enumerate(chunks_for_citations[:5], 1):
                text = c.get("text", "")[:200]
                meta = c.get("metadata", {})
                source = meta.get("file_name", "")
                page = meta.get("page")
                ref = f"{source}, S. {page}" if page else source
                lines.append(f"[{i}] ({ref}) {text}")
            answer = "\n".join(lines)
            sufficient = True
        else:
            answer = (
                "Zu dieser Anfrage wurden keine passenden Dokumente gefunden. "
                "Versuchen Sie eine andere Formulierung oder laden Sie relevante Dokumente hoch."
            )
            sufficient = False

    # Clean up answer – remove agent framework artifacts
    # 1. Remove FINAL_ANSWER({ "answer": " prefix (with optional markdown bold ** etc.)
    answer = re.sub(r'^[\s*]*FINAL_ANSWER\s*\(\s*\{?\s*', "", answer)
    answer = re.sub(r'^[\s*]*"answer"\s*:\s*"?\s*', "", answer)
    # 2. Remove trailing JSON artifacts: ", "extras": {...} }) etc.
    answer = re.sub(r'["\s]*,\s*"extras"\s*:\s*\{.*$', "", answer, flags=re.DOTALL)
    # 3. Remove trailing }) or } from leaked JSON
    answer = re.sub(r'\s*\}?\s*\)\s*$', "", answer)
    # 4. Remove lines starting with THOUGHT/ACTION/OBSERVATION/FINAL_ANSWER
    answer = re.sub(r"^[ \t*]*(?:THOUGHT|ACTION|OBSERVATION|FINAL_ANSWER)[:({\s].*$", "", answer, flags=re.MULTILINE).strip()
    # 5. Remove leftover separators and leading/trailing quotes
    answer = answer.strip('" \n')
    answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
    if not answer:
        answer = (
            "Die Anfrage konnte nicht vollständig beantwortet werden. "
            "Bitte präzisieren Sie Ihre Frage."
        )

    # Citation mapping
    citations = map_citations(answer, chunks_for_citations)
    answer = strip_unresolved_refs(answer, citations)

    # Memory update (non-blocking). Sub-agents run with persist_memory=False:
    # the manager owns a single LLM-moderated memory update per user query, so
    # the shared use-case memory doesn't get spammed by every sub-agent (and
    # concurrent sub-agents don't race on the same row).
    if persist_memory:
        asyncio.create_task(_update_memory(use_case, query, answer))

    result = {
        "answer": answer,
        "citations": citations,
        "agent_steps": steps,
        "system_prompt": full_system,
        "enriched_query": enriched_query,
        "use_case_extras": use_case_extras,
        "sufficient": sufficient,
        "session_id": session_id,
        "use_case": use_case,
        "searched_collections": sorted(searched_collections),
    }
    if on_event:
        await on_event({"type": "final", **result})
    return result

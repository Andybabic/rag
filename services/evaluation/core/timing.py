"""Summarise sequential pipeline phases so the UI can name the bottleneck."""


def _ms(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def step_timing_children(steps: list[dict] | None) -> list[dict]:
    """One child phase per ReAct step, with embed/search/rerank nested."""
    children: list[dict] = []
    for step in steps or []:
        action = str(step.get("action") or "?").strip() or "?"
        num = step.get("step")
        label = f"{num} {action}" if num is not None else action
        parts: list[dict] = []
        for key, part_label in (
            ("embed_ms", "Embed"),
            ("search_ms", "Suche"),
            ("rerank_ms", "Rerank"),
        ):
            part_ms = _ms(step.get(key))
            if part_ms > 0:
                parts.append({
                    "id": f"{label}-{key}",
                    "label": part_label,
                    "ms": part_ms,
                })
        child: dict = {
            "id": f"step-{num}-{action}",
            "label": label,
            "ms": _ms(step.get("duration_ms")),
            "llm_ms": step.get("llm_ms"),
        }
        if parts:
            child["children"] = parts
        children.append(child)
    return children


def subagent_phase_children(subagents: list[dict] | None) -> list[dict]:
    """Per sub-agent wall clock + LLM sum + ReAct step breakdown."""
    children: list[dict] = []
    for sub in subagents or []:
        steps = sub.get("agent_steps") or []
        llm_ms = sum(_ms(s.get("llm_ms")) for s in steps)
        child: dict = {
            "id": sub.get("subagent_id") or sub.get("role") or "sub",
            "label": sub.get("role_label") or sub.get("role") or "Sub-Agent",
            "ms": _ms(sub.get("duration_ms")),
            "llm_ms": llm_ms or None,
            "children": step_timing_children(steps),
        }
        children.append(child)
    return children


def _pick_slowest(phases: list[dict]) -> dict | None:
    nonempty = [p for p in phases if _ms(p.get("ms")) > 0]
    if not nonempty:
        return None
    return max(nonempty, key=lambda p: _ms(p.get("ms")))


def _drill_bottleneck(phase: dict, total_ms: int) -> dict:
    """Walk into children while one child is at least half of this phase."""
    current = phase
    while True:
        children = current.get("children") or []
        child = _pick_slowest(children)
        if child is None:
            break
        parent_ms = _ms(current.get("ms"))
        if parent_ms <= 0 or _ms(child.get("ms")) * 2 < parent_ms:
            break
        current = child
    ms = _ms(current.get("ms"))
    return {
        "id": current.get("id"),
        "label": current.get("label"),
        "ms": current.get("ms"),
        "share_pct": round(100 * ms / total_ms) if total_ms else 0,
    }


def timing_report(phases: list[dict], total_ms: int) -> dict:
    top = _pick_slowest(phases)
    bottleneck = _drill_bottleneck(top, total_ms) if top and total_ms else None
    return {"total_ms": total_ms, "phases": phases, "bottleneck": bottleneck}

"""The gate between an LLM's text and a running container.

These cover the decisions the plugin layer makes *before* anything executes,
because that is where the whole design lives: a call that does not resolve
must not run, a function marked ``ask for permission`` must not run without an
explicit yes, and neither a denial nor a crash may cost the user an answer
that is already finished.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import core.permissions as permissions
import core.skill_functions as sf
import pytest


@dataclass
class _Fn:
    name: str
    signature: str = ""
    description: str = ""
    params: tuple = ()
    ask_for_permission: bool = False
    allowed_domains: tuple = ()
    timeout_ms: int = 5000
    memory_mb: int = 128
    skill_name: str = "wetter"

    @property
    def qualified_name(self):
        return f"{self.skill_name}.{self.name}"


@dataclass
class _Bundle:
    function: _Fn
    skill_id: str = "11111111-1111-1111-1111-111111111111"
    skill_name: str = "wetter"
    use_case: str = "neumann"
    language: str = "python"
    code: str = "def aktuell(ort):\n    return ort\n"
    variables: dict = None

    def __post_init__(self):
        if self.variables is None:
            self.variables = {}


@pytest.fixture(autouse=True)
def _quiet_audit():
    """The audit writer needs a DB; every test here asserts on behaviour."""
    with patch.object(sf, "log_skill_execution", AsyncMock(return_value=None)):
        yield


@pytest.fixture
def known_functions():
    with patch.object(
        sf, "list_skill_functions", AsyncMock(return_value=[_Fn(name="aktuell")])
    ):
        yield


async def _events_sink():
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    return events, on_event


# ── resolution ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_answer_without_calls_is_returned_untouched(known_functions):
    answer = "Die Wartung erfolgt jährlich [1]."
    result, traces = await sf.apply_skill_functions(
        answer, use_case="neumann", session_id="s"
    )
    assert result == answer
    assert traces == []


@pytest.mark.anyio
async def test_unresolvable_call_is_never_executed(known_functions):
    """A call the database cannot resolve is marked, not attempted.

    This is the "nichts auf Verdacht" rule: without it, a model inventing a
    plausible-looking function name would reach the sandbox.
    """
    sandbox = AsyncMock()
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=None)), \
         patch.object(sf, "_call_sandbox", sandbox):
        result, traces = await sf.apply_skill_functions(
            "Wetter: wetter.aktuell(Fürth)", use_case="neumann", session_id="s"
        )

    sandbox.assert_not_awaited()
    assert traces[0]["status"] == "unresolved"
    assert "wetter.aktuell" in result and "⚠️" in result


@pytest.mark.anyio
async def test_successful_call_is_substituted_inline(known_functions):
    bundle = _Bundle(function=_Fn(name="aktuell"))
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", AsyncMock(return_value={
             "ok": True, "result": "14 °C, leicht bewölkt", "duration_ms": 320,
         })):
        result, traces = await sf.apply_skill_functions(
            'Aktuell in Fürth: wetter.aktuell("Fürth")',
            use_case="neumann", session_id="s",
        )

    assert result == "Aktuell in Fürth: 14 °C, leicht bewölkt"
    assert traces[0]["status"] == "ok"


@pytest.mark.anyio
async def test_skill_variables_reach_the_sandbox_but_not_the_trace(known_functions):
    """Variables are execution input, not output.

    An API key handed to a function must not come back in the trace the
    frontend renders or in anything the LLM can read.
    """
    bundle = _Bundle(
        function=_Fn(name="aktuell"),
        variables={"OPENMETEO_API_KEY": "super-secret-key"},
    )
    sandbox = AsyncMock(return_value={"ok": True, "result": "12 °C"})
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", sandbox):
        result, traces = await sf.apply_skill_functions(
            "wetter.aktuell(Fürth)", use_case="neumann", session_id="s"
        )

    assert sandbox.await_args.args[0].variables["OPENMETEO_API_KEY"] == "super-secret-key"
    assert "super-secret-key" not in result
    assert "super-secret-key" not in repr(traces)


# ── consent ──────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_function_without_marker_runs_without_asking(known_functions):
    events, on_event = await _events_sink()
    bundle = _Bundle(function=_Fn(name="aktuell", ask_for_permission=False))
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", AsyncMock(return_value={"ok": True, "result": "ok"})):
        await sf.apply_skill_functions(
            "wetter.aktuell(X)", use_case="neumann", session_id="s", on_event=on_event
        )

    assert not any(e["type"] == "permission_request" for e in events)


@pytest.mark.anyio
async def test_marked_function_waits_for_approval_before_executing(known_functions):
    """The container must not start until the user has said yes."""
    events, on_event = await _events_sink()
    bundle = _Bundle(function=_Fn(name="aktuell", ask_for_permission=True))
    sandbox = AsyncMock(return_value={"ok": True, "result": "erledigt"})

    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", sandbox):
        task = asyncio.create_task(
            sf.apply_skill_functions(
                "wetter.aktuell(X)", use_case="neumann", session_id="s",
                on_event=on_event,
            )
        )
        # Let the pipeline reach the prompt and block there.
        for _ in range(50):
            await asyncio.sleep(0.001)
            if any(e["type"] == "permission_request" for e in events):
                break

        request = next(e for e in events if e["type"] == "permission_request")
        sandbox.assert_not_awaited(), "executed before the user answered"

        assert permissions.resolve(request["call_id"], True, session_id="s")
        result, traces = await asyncio.wait_for(task, timeout=5)

    sandbox.assert_awaited_once()
    assert result == "erledigt"
    assert traces[0]["status"] == "ok" and traces[0]["permission_required"] is True


@pytest.mark.anyio
async def test_denied_function_does_not_execute(known_functions):
    events, on_event = await _events_sink()
    bundle = _Bundle(function=_Fn(name="aktuell", ask_for_permission=True))
    sandbox = AsyncMock()

    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", sandbox):
        task = asyncio.create_task(
            sf.apply_skill_functions(
                "wetter.aktuell(X)", use_case="neumann", session_id="s",
                on_event=on_event,
            )
        )
        for _ in range(50):
            await asyncio.sleep(0.001)
            if any(e["type"] == "permission_request" for e in events):
                break
        request = next(e for e in events if e["type"] == "permission_request")
        permissions.resolve(request["call_id"], False, session_id="s")
        result, traces = await asyncio.wait_for(task, timeout=5)

    sandbox.assert_not_awaited()
    assert traces[0]["status"] == "denied"
    assert "nicht freigegeben" in result


@pytest.mark.anyio
async def test_timeout_counts_as_denial(known_functions):
    bundle = _Bundle(function=_Fn(name="aktuell", ask_for_permission=True))
    sandbox = AsyncMock()
    events, on_event = await _events_sink()

    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", sandbox), \
         patch.object(sf, "PERMISSION_TIMEOUT_S", 0.05):
        _, traces = await sf.apply_skill_functions(
            "wetter.aktuell(X)", use_case="neumann", session_id="s", on_event=on_event
        )

    sandbox.assert_not_awaited()
    assert traces[0]["status"] == "denied"


@pytest.mark.anyio
async def test_marked_function_is_refused_when_there_is_no_way_to_ask(known_functions):
    """No event channel (the public API) means no consent, so no execution."""
    bundle = _Bundle(function=_Fn(name="aktuell", ask_for_permission=True))
    sandbox = AsyncMock()
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", sandbox):
        _, traces = await sf.apply_skill_functions(
            "wetter.aktuell(X)", use_case="neumann", session_id="s", on_event=None
        )

    sandbox.assert_not_awaited()
    assert traces[0]["status"] == "denied"


@pytest.mark.anyio
async def test_approval_from_another_session_is_ignored():
    """One conversation must not be able to approve another's execution."""
    pending = permissions.register(
        session_id="session-a", use_case="neumann", skill="wetter",
        function="aktuell", args=(),
    )
    assert permissions.resolve(pending.call_id, True, session_id="session-b") is False
    assert permissions.resolve(pending.call_id, True, session_id="session-a") is True


# ── failure containment ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_sandbox_error_is_marked_without_losing_the_answer(known_functions):
    bundle = _Bundle(function=_Fn(name="aktuell"))
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", AsyncMock(return_value={
             "ok": False, "error": "Zeitlimit von 5000 ms überschritten.",
             "timed_out": True, "duration_ms": 5200,
         })):
        result, traces = await sf.apply_skill_functions(
            "Text davor. wetter.aktuell(X) Text danach.",
            use_case="neumann", session_id="s",
        )

    assert traces[0]["status"] == "timeout"
    assert result.startswith("Text davor.") and result.endswith("Text danach.")


@pytest.mark.anyio
async def test_unexpected_exception_does_not_break_the_answer(known_functions):
    with patch.object(
        sf, "get_execution_bundle", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        result, traces = await sf.apply_skill_functions(
            "Wetter: wetter.aktuell(X)", use_case="neumann", session_id="s"
        )

    assert traces[0]["status"] == "error"
    assert "Wetter:" in result


@pytest.mark.anyio
async def test_disabled_flag_short_circuits_everything():
    lister = AsyncMock()
    with patch.object(sf, "ENABLED", False), \
         patch.object(sf, "list_skill_functions", lister):
        result, traces = await sf.apply_skill_functions(
            "wetter.aktuell(X)", use_case="neumann", session_id="s"
        )
    lister.assert_not_awaited()
    assert result == "wetter.aktuell(X)" and traces == []


# ── mehrere Skills in einer Antwort ──────────────────────────────────────────


def _bundle_for(skill: str, function: str, *, language: str = "python", **kw) -> _Bundle:
    fn = _Fn(name=function, skill_name=skill, **kw)
    return _Bundle(function=fn, skill_name=skill, language=language,
                   skill_id=f"{skill}-0000-0000-0000-000000000000")


@pytest.fixture
def three_skills():
    """A use case carrying three skills, one of them permission-gated."""
    functions = [
        _Fn(name="aktuell", skill_name="wetter"),
        _Fn(name="bestand", skill_name="lager"),
        _Fn(name="auftrag_anlegen", skill_name="wartung", ask_for_permission=True),
    ]
    with patch.object(sf, "list_skill_functions", AsyncMock(return_value=functions)):
        yield functions


@pytest.mark.anyio
async def test_calls_to_different_skills_are_each_resolved_and_substituted(three_skills):
    """One answer, two skills, two results — each in its own place."""
    bundles = {
        ("wetter", "aktuell"): _bundle_for("wetter", "aktuell"),
        ("lager", "bestand"): _bundle_for("lager", "bestand", language="javascript"),
    }

    async def resolve(use_case, skill, function):
        return bundles.get((skill, function))

    async def sandbox(bundle, args):
        return {"ok": True, "result": {"wetter": "14 °C", "lager": "7 Stück"}[bundle.skill_name]}

    with patch.object(sf, "get_execution_bundle", AsyncMock(side_effect=resolve)), \
         patch.object(sf, "_call_sandbox", AsyncMock(side_effect=sandbox)):
        result, traces = await sf.apply_skill_functions(
            'Wetter: wetter.aktuell("Fürth"). Lager: lager.bestand("M8").',
            use_case="neumann", session_id="s",
        )

    assert result == "Wetter: 14 °C. Lager: 7 Stück."
    assert [(t["skill"], t["status"]) for t in traces] == [
        ("wetter", "ok"), ("lager", "ok")
    ]


@pytest.mark.anyio
async def test_each_skill_runs_in_its_own_language(three_skills):
    """Python and JavaScript skills coexist; the language travels per call."""
    bundles = {
        ("wetter", "aktuell"): _bundle_for("wetter", "aktuell"),
        ("lager", "bestand"): _bundle_for("lager", "bestand", language="javascript"),
    }
    seen: list[tuple[str, str]] = []

    async def resolve(use_case, skill, function):
        return bundles.get((skill, function))

    async def sandbox(bundle, args):
        seen.append((bundle.skill_name, bundle.language))
        return {"ok": True, "result": "x"}

    with patch.object(sf, "get_execution_bundle", AsyncMock(side_effect=resolve)), \
         patch.object(sf, "_call_sandbox", AsyncMock(side_effect=sandbox)):
        await sf.apply_skill_functions(
            "wetter.aktuell(A) lager.bestand(B)", use_case="neumann", session_id="s"
        )

    assert sorted(seen) == [("lager", "javascript"), ("wetter", "python")]


@pytest.mark.anyio
async def test_one_skills_failure_does_not_affect_the_others(three_skills):
    """Skills are independent; a broken one must not take a working one down."""
    async def resolve(use_case, skill, function):
        if skill == "lager" and function == "bestand":
            return _bundle_for("lager", "bestand")
        if skill == "wetter" and function == "aktuell":
            return _bundle_for("wetter", "aktuell")
        return None

    async def sandbox(bundle, args):
        if bundle.skill_name == "lager":
            return {"ok": False, "error": "ERP nicht erreichbar", "duration_ms": 12}
        return {"ok": True, "result": "14 °C"}

    with patch.object(sf, "get_execution_bundle", AsyncMock(side_effect=resolve)), \
         patch.object(sf, "_call_sandbox", AsyncMock(side_effect=sandbox)):
        result, traces = await sf.apply_skill_functions(
            "A: wetter.aktuell(X) B: lager.bestand(Y) C: wetter.gibtsnicht(Z)",
            use_case="neumann", session_id="s",
        )

    assert [(t["skill"], t["function"], t["status"]) for t in traces] == [
        ("wetter", "aktuell", "ok"),
        ("lager", "bestand", "error"),
        ("wetter", "gibtsnicht", "unresolved"),
    ]
    assert result.startswith("A: 14 °C")
    assert "⚠️" in result


@pytest.mark.anyio
async def test_only_the_gated_skill_asks_while_the_others_just_run(three_skills):
    """Consent is per function, not per answer.

    The weather and inventory calls must not be held hostage by the maintenance
    call's dialog, and the maintenance call must still not run without a yes.
    """
    events, on_event = await _events_sink()
    ran: list[str] = []

    async def resolve(use_case, skill, function):
        gated = skill == "wartung"
        return _bundle_for(skill, function, ask_for_permission=gated)

    async def sandbox(bundle, args):
        ran.append(bundle.skill_name)
        return {"ok": True, "result": bundle.skill_name}

    with patch.object(sf, "get_execution_bundle", AsyncMock(side_effect=resolve)), \
         patch.object(sf, "_call_sandbox", AsyncMock(side_effect=sandbox)):
        task = asyncio.create_task(
            sf.apply_skill_functions(
                "wetter.aktuell(A) lager.bestand(B) wartung.auftrag_anlegen(C)",
                use_case="neumann", session_id="s", on_event=on_event,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.001)
            if any(e["type"] == "permission_request" for e in events):
                break

        requests = [e for e in events if e["type"] == "permission_request"]
        assert len(requests) == 1, "only the gated skill may raise a dialog"
        assert requests[0]["skill"] == "wartung"
        assert sorted(ran) == ["lager", "wetter"], "ungated skills ran already"

        permissions.resolve(requests[0]["call_id"], True, session_id="s")
        _, traces = await asyncio.wait_for(task, timeout=5)

    assert sorted(ran) == ["lager", "wartung", "wetter"]
    assert {t["skill"]: t["status"] for t in traces} == {
        "wetter": "ok", "lager": "ok", "wartung": "ok"
    }


@pytest.mark.anyio
async def test_two_gated_skills_are_asked_one_after_another():
    """Never two open consent dialogs in one bubble."""
    events, on_event = await _events_sink()
    known = [
        _Fn(name="auftrag_anlegen", skill_name="wartung", ask_for_permission=True),
        _Fn(name="senden", skill_name="mail", ask_for_permission=True),
    ]

    async def resolve(use_case, skill, function):
        return _bundle_for(skill, function, ask_for_permission=True)

    with patch.object(sf, "list_skill_functions", AsyncMock(return_value=known)), \
         patch.object(sf, "get_execution_bundle", AsyncMock(side_effect=resolve)), \
         patch.object(sf, "_call_sandbox", AsyncMock(return_value={"ok": True, "result": "x"})):
        task = asyncio.create_task(
            sf.apply_skill_functions(
                "wartung.auftrag_anlegen(A) mail.senden(B)",
                use_case="neumann", session_id="s", on_event=on_event,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.001)
            if any(e["type"] == "permission_request" for e in events):
                break
        assert len([e for e in events if e["type"] == "permission_request"]) == 1

        first = next(e for e in events if e["type"] == "permission_request")
        permissions.resolve(first["call_id"], True, session_id="s")

        for _ in range(200):
            await asyncio.sleep(0.001)
            pending = [e for e in events
                       if e["type"] == "permission_request" and e["call_id"] != first["call_id"]]
            if pending:
                break
        assert pending, "the second dialog only appears after the first is answered"
        permissions.resolve(pending[0]["call_id"], False, session_id="s")

        _, traces = await asyncio.wait_for(task, timeout=5)

    assert [t["status"] for t in traces] == ["ok", "denied"]


@pytest.mark.anyio
async def test_a_skill_not_in_this_use_case_is_never_looked_up(three_skills):
    """The known-skill filter is per use case, not global."""
    resolver = AsyncMock(return_value=None)
    with patch.object(sf, "get_execution_bundle", resolver):
        result, traces = await sf.apply_skill_functions(
            "fremd.funktion(X)", use_case="neumann", session_id="s"
        )
    resolver.assert_not_awaited()
    assert traces == [] and result == "fremd.funktion(X)"


# ── SKILL als aktive Action ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_skill_action_returns_the_value_as_an_observation(three_skills):
    """The active path: the agent calls, the value comes back to reason with.

    This is what the text-pattern path cannot do — by the time a call written
    into the answer is substituted, the answer is already finished.
    """
    bundle = _bundle_for("wetter", "aktuell")
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox",
                      AsyncMock(return_value={"ok": True, "result": "14 °C, bewölkt"})):
        observation = await sf.execute_named_function(
            "wetter", "aktuell", ["Fürth"], use_case="neumann", session_id="s"
        )

    assert observation == "14 °C, bewölkt"


@pytest.mark.anyio
async def test_skill_action_still_asks_before_a_gated_function_runs(three_skills):
    """The gate has to hold on this path too.

    Two execution routes with separate ideas about when to ask permission is
    how one of them ends up not asking.
    """
    events, on_event = await _events_sink()
    bundle = _bundle_for("wartung", "auftrag_anlegen", ask_for_permission=True)
    sandbox = AsyncMock()

    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", sandbox):
        task = asyncio.create_task(sf.execute_named_function(
            "wartung", "auftrag_anlegen", ["Anlage 7"],
            use_case="neumann", session_id="s", on_event=on_event,
        ))
        for _ in range(200):
            await asyncio.sleep(0.001)
            if any(e["type"] == "permission_request" for e in events):
                break

        request = next(e for e in events if e["type"] == "permission_request")
        sandbox.assert_not_awaited()
        permissions.resolve(request["call_id"], False, session_id="s")
        observation = await asyncio.wait_for(task, timeout=5)

    sandbox.assert_not_awaited()
    assert "nicht freigegeben" in observation


@pytest.mark.anyio
async def test_unknown_function_lists_what_does_exist(three_skills):
    """A wrong name is a prompting problem; the observation should fix it."""
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=None)):
        observation = await sf.execute_named_function(
            "wetter", "regenwahrscheinlichkeit", ["Fürth"],
            use_case="neumann", session_id="s",
        )

    assert "gibt es nicht" in observation
    assert "wetter.aktuell" in observation, "the agent needs the valid names"


@pytest.mark.anyio
async def test_sandbox_failure_comes_back_as_a_readable_observation(three_skills):
    bundle = _bundle_for("wetter", "aktuell")
    with patch.object(sf, "get_execution_bundle", AsyncMock(return_value=bundle)), \
         patch.object(sf, "_call_sandbox", AsyncMock(return_value={
             "ok": False, "error": "Ort 'Xyz' wurde nicht gefunden.", "duration_ms": 12,
         })):
        observation = await sf.execute_named_function(
            "wetter", "aktuell", ["Xyz"], use_case="neumann", session_id="s"
        )

    assert "Ort 'Xyz' wurde nicht gefunden." in observation

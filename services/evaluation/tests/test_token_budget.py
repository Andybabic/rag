"""Per-call output budgets that bound a runaway generation."""

from __future__ import annotations

import pytest

from core.llm import (
    TOKENS_AGENT_STEP,
    TOKENS_COMPLIANCE,
    TOKENS_PLAN,
    TOKENS_SYNTHESIS,
    _effective_max_tokens,
)


def test_call_site_budget_applies_when_nothing_is_configured():
    """The default path: no MAX_TOKENS set, so the call site decides."""
    assert _effective_max_tokens(None, TOKENS_SYNTHESIS) == TOKENS_SYNTHESIS


def test_configured_ceiling_applies_when_call_site_has_no_budget():
    assert _effective_max_tokens(512, None) == 512


def test_operator_ceiling_is_never_raised_by_a_call_site():
    """MAX_TOKENS is an operator-set ceiling — a larger call-site budget must
    not overrule it, or the setting would silently stop meaning anything."""
    assert _effective_max_tokens(512, TOKENS_SYNTHESIS) == 512


def test_call_site_is_not_forced_up_to_the_ceiling():
    """A compliance verdict needs a few hundred tokens; a generous global
    ceiling must not hand it a licence to generate 4000."""
    assert _effective_max_tokens(4000, TOKENS_COMPLIANCE) == TOKENS_COMPLIANCE


def test_unbounded_when_neither_side_sets_a_budget():
    assert _effective_max_tokens(None, None) is None


@pytest.mark.parametrize("budget", [TOKENS_AGENT_STEP, TOKENS_SYNTHESIS])
def test_prose_budgets_clear_the_observed_answer_length(budget):
    """These two carry user-facing prose. Observed answers run to roughly 600
    tokens, so a guard at that level would start truncating real output."""
    assert budget > 1000


@pytest.mark.parametrize("budget", [TOKENS_PLAN, TOKENS_COMPLIANCE])
def test_structured_budgets_clear_their_json_payload(budget):
    """The planner and the compliance check emit short JSON, not prose — a few
    hundred tokens is ample. They only need to stay clear of *that* size."""
    assert budget >= 500


def test_synthesis_has_the_largest_budget():
    """The synthesizer writes the user-facing answer; the planner and the
    compliance check only emit short JSON. Ordering them wrong would clip the
    answer while leaving the cheap calls unbounded."""
    assert TOKENS_SYNTHESIS > TOKENS_PLAN
    assert TOKENS_SYNTHESIS > TOKENS_COMPLIANCE

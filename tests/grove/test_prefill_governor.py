"""Sprint 77.0a local-prefill-governor-v1 — governor config surface.

The pre-flight memory governor lives in ``run_agent._run_turn_generator`` (a
sibling of the preflight-compression check) and is keyed on the resolved tier's
``TierBudget.prefill_ceiling_tokens``. Its control flow (yield EscalationRequest
on over-ceiling; PrefillCeilingExceeded on the deny branch; record-only in
measurement mode) is integration-level and is validated live under guard in
Sprint 77.1. What is unit-testable here — and what the Prime Directive requires
to be fail-loud — is the CONFIG surface: the ceiling parses to a positive int,
absence is None (cloud no-op), and malformed values raise at load.
"""

from pathlib import Path

import pytest

from grove.tier_budget import (
    PrefillCeilingExceeded,
    TierBudget,
    TierBudgetMissing,
    _parse_tier_budget,
)

_VALID_GROUPS = frozenset({"core", "exploratory", "analysis"})


def _spec(**over):
    base = {"context": [], "tools": {"allow_groups": ["core"], "exclude_mcp": []}}
    base.update(over)
    return base


def test_ceiling_positive_int_parses():
    tb = _parse_tier_budget(
        "T2", _spec(prefill_ceiling_tokens=8000), Path("x"), _VALID_GROUPS
    )
    assert tb.prefill_ceiling_tokens == 8000


def test_ceiling_absent_is_none_cloud_noop():
    # No prefill_ceiling_tokens key — the governor no-ops for this tier
    # (every cloud tier: an unbounded window).
    tb = _parse_tier_budget("T3", _spec(), Path("x"), _VALID_GROUPS)
    assert tb.prefill_ceiling_tokens is None


def test_tierbudget_default_ceiling_is_none():
    # Direct construction (PERMISSIVE_TIER_BUDGET path, tests, legacy callers)
    # defaults to None — no governor unless explicitly configured.
    tb = TierBudget(context=(), tools=None)  # type: ignore[arg-type]
    assert tb.prefill_ceiling_tokens is None


@pytest.mark.parametrize("bad", [0, -5, True, False, "8000", 1.5, [8000]])
def test_ceiling_malformed_fails_loud(bad):
    # D7 / Prime Directive: a present-but-malformed ceiling raises at load —
    # never silently ignored. ``bool`` is rejected though isinstance(True, int).
    with pytest.raises(ValueError):
        _parse_tier_budget(
            "T2", _spec(prefill_ceiling_tokens=bad), Path("x"), _VALID_GROUPS
        )


def test_prefill_ceiling_exceeded_is_fail_loud_runtimeerror():
    # The deny-branch exception must be a real RuntimeError (a raise, not an
    # assert that strips under python -O), in the same Prime-Directive family
    # as TierBudgetMissing.
    assert issubclass(PrefillCeilingExceeded, RuntimeError)
    assert issubclass(TierBudgetMissing, RuntimeError)

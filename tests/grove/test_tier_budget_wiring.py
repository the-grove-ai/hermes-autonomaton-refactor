"""Tier-budget carrier wiring — Sprint 73 declarative-jit-budget-v1 (Phase 4a).

Unit-tests the Dispatcher carrier methods (_apply_tier_budget /
_maybe_recompose_for_tier / _get_tier_budgets) in isolation: SINGLE SOURCE
(both carriers from one budget), FAIL-LOUD raise on a routed tier with no
budget, additive cache-friendly recompose, and the escalation-hot-swap path.

The Dispatcher is built via __new__ so the carrier logic is exercised without
plugin/MCP discovery or a real compose (the recompose seam is spied). The lazy
loader's fail-loud-at-load is covered by test_tier_budget.py (Phase 1); here the
budget map is injected.
"""

from __future__ import annotations

import types

import pytest

from grove.dispatcher import Dispatcher
from grove.tier_budget import TierBudget, TierBudgetMissing


def _budget(context):
    return TierBudget(context=tuple(context))


# K6 (A-goalrec-tests ruling) — representative gateable block swapped
# goal_record -> skills_index (goal_record left GATEABLE_CONTEXT_BLOCKS).
BUDGETS = {
    "T1": _budget([]),
    "T2": _budget(["skills_index"]),
    "T3": _budget(["claude_contract", "skills_index"]),
}


@pytest.fixture
def disp():
    """A Dispatcher shell with carrier state only (no heavy __init__)."""
    d = Dispatcher.__new__(Dispatcher)
    d._tier_budgets_cache = dict(BUDGETS)
    d._last_applied_tier_context_blocks = object()  # 'unset' sentinel stand-in
    d.agent = None
    return d


def _spy_recompose(d):
    calls = []
    d.recompose_system_prompt = lambda **kw: calls.append("recompose")
    return calls


# ── SINGLE SOURCE ──────────────────────────────────────────────────────────


def test_single_source_both_carriers_from_one_budget(disp):
    _spy_recompose(disp)
    agent = types.SimpleNamespace()
    disp.agent = agent
    disp._apply_tier_budget(agent, "T2")
    assert agent._tier_budget is BUDGETS["T2"]                        # tools carrier
    assert agent._tier_context_blocks == frozenset({"skills_index"})  # context carrier
    # both derive from the SAME TierBudget — they cannot disagree
    assert agent._tier_context_blocks == frozenset(agent._tier_budget.context)


# ── FAIL LOUD, NOT EAGER ───────────────────────────────────────────────────


def test_missing_budget_raises_named_error_not_eager(disp):
    _spy_recompose(disp)
    agent = types.SimpleNamespace()
    disp.agent = agent
    with pytest.raises(TierBudgetMissing, match=r"T9.*no tier_budgets entry"):
        disp._apply_tier_budget(agent, "T9")
    # carriers are NOT left in a silently-eager state on the raise path
    assert not hasattr(agent, "_tier_budget") or agent._tier_budget is None


def test_no_routed_tier_is_noop_legacy_path(disp):
    calls = _spy_recompose(disp)
    agent = types.SimpleNamespace(_tier_budget="KEEP", _tier_context_blocks="KEEP")
    disp.agent = agent
    disp._apply_tier_budget(agent, None)
    disp._apply_tier_budget(agent, "")
    assert agent._tier_budget == "KEEP" and agent._tier_context_blocks == "KEEP"
    assert calls == []  # no tier ⇒ no recompose


# ── additive, cache-friendly recompose ─────────────────────────────────────


def test_carrier_change_recompose_is_cache_friendly(disp):
    calls = _spy_recompose(disp)
    agent = types.SimpleNamespace()
    disp.agent = agent
    disp._apply_tier_budget(agent, "T2")   # first apply → recompose
    disp._apply_tier_budget(agent, "T2")   # unchanged tier → skip
    disp._apply_tier_budget(agent, "T3")   # changed → recompose
    disp._apply_tier_budget(agent, "T3")   # unchanged → skip
    assert calls == ["recompose", "recompose"]


def test_recompose_is_additive_not_gated_by_tier_skip(disp):
    # recompose_system_prompt must stay independently callable (Sprint 36
    # compression / session_register triggers). The carrier-change skip governs
    # ONLY _maybe_recompose_for_tier — never recompose_system_prompt itself, so
    # a register change with an unchanged tier still recomposes.
    compose_calls = []

    def _fake_compose(agent, **kw):
        compose_calls.append(agent)
        agent._composed_system_prompt = "PROMPT"

    disp._compose_and_inject_system_prompt = _fake_compose
    agent = types.SimpleNamespace()
    disp.agent = agent
    disp._apply_tier_budget(agent, "T2")     # tier trigger → 1 compose
    disp.recompose_system_prompt()           # Sprint 36 trigger → still composes
    disp.recompose_system_prompt()           # and again — never suppressed
    assert len(compose_calls) == 3


# ── escalation hot-swap path (new agent ≠ self.agent) ──────────────────────


def test_hot_swap_new_agent_composed_directly(disp):
    compose_calls = []

    def _fake_compose(agent, **kw):
        compose_calls.append(agent)
        agent._composed_system_prompt = "PROMPT"

    disp._compose_and_inject_system_prompt = _fake_compose
    disp.agent = types.SimpleNamespace(tag="old")     # the retiring T2 shell
    new_agent = types.SimpleNamespace(tag="new")
    disp._apply_tier_budget(new_agent, "T3")          # new_agent is not self.agent
    assert new_agent._tier_budget is BUDGETS["T3"]
    assert new_agent._tier_context_blocks == frozenset(
        {"claude_contract", "skills_index"}
    )
    assert compose_calls == [new_agent]               # composed the NEW agent

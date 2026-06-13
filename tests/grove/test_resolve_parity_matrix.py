"""GRV-009 E5 C-RESOLVE — the registry-driven resolver parity proof (parts 2 & 3).

The swap moved native tool admission from the tool_groups.yaml materialization to
the capability registry (record disclosure/intents + bindings, intersected with
the tier's allow_groups budget — A8). This module proves that swap three ways
(part 1, the 192-cell golden surface match, lives in test_offer_parity_snapshot):

2. STATE x CONFIG INTERSECTION MATRIX — for every (tier x intent_class x
   complexity) cell, BOTH entrypoints (the tier-aware resolve_tools_for_tier and
   the tier-unaware resolve_tool_set) yield EXACTLY what the legacy group-level
   logic yields. The oracle is computed LIVE from the still-present legacy
   helpers (_resolve_intent_groups + _materialize over tool_groups) — a moving
   oracle, stronger than the frozen golden.

3. DISCLOSURE-MODE INVARIANTS — asserted explicitly in code, not emergent from
   the surface match: a complexity record is ABSENT on a simple T3 turn; a
   fallback record is ABSENT on every known-intent cell (incl. T3|research|
   complex) and PRESENT only in the maximal unknown-intent fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.classify import COMPLEXITY_SIGNALS, INTENT_CLASSES
from grove.context_budget import (
    load_taxonomy,
    resolve_tool_set,
    resolve_tools_for_tier,
)
from grove.tier_budget import load_tier_budgets

_REPO = Path(__file__).resolve().parents[2]
_CFG = _REPO / "config" / "routing.config.yaml"
_TAX = _REPO / "config" / "tool_groups.yaml"

BUDGETS = load_tier_budgets(_CFG, taxonomy_path=_TAX)
TAXONOMY = load_taxonomy(_TAX)


# GRV-009 E5 C-RETIRE — the production _resolve_intent_groups / _materialize
# (the tool_groups.yaml tool->group readers) are retired from the resolver path.
# The moving oracle keeps a SELF-CONTAINED copy of that legacy group-level logic,
# reading the still-present tool_groups.yaml directly, so it still proves the
# registry resolver reproduces the legacy surface — independent of production.
def _legacy_intent_groups(intent, cx):
    if intent is None or intent == "unknown":
        return None
    groups = {"core"}
    if intent in (TAXONOMY.get("domain_chunks") or {}):
        groups.add(intent)
    if cx in ("complex", "novel"):
        groups.add("exploratory")
    return groups


def _legacy_materialize(groups):
    names = set()
    domain = TAXONOMY.get("domain_chunks") or {}
    for g in groups:
        if g == "core":
            names.update(TAXONOMY.get("core", []))
        elif g == "exploratory":
            names.update(TAXONOMY.get("exploratory", []))
        elif g in domain:
            names.update(domain[g])
    return names


def _native_surface():
    from hermes_cli.tools_config import _cli_registry

    reg = _cli_registry()
    names = sorted(n for n in {e.name for e in reg._tools.values()} if not n.startswith("mcp_"))
    return [{"type": "function", "function": {"name": n}} for n in names]


TOOLS = _native_surface()
INTENTS = list(INTENT_CLASSES) + [None]  # None == unknown / maximal fallback


def _names(res):
    return {t["function"]["name"] for t in res.tools}


def _legacy_oracle(intent, cx, tier_budget):
    """The legacy group-level native surface (the logic C-RESOLVE replaced)."""
    allow = set(tier_budget.tools.allow_groups)
    wildcard = "*" in allow
    groups = _legacy_intent_groups(intent, cx)
    native = {t["function"]["name"] for t in TOOLS}
    if groups is None:                         # unknown / maximal fallback
        if wildcard:
            return set(native)
        return _legacy_materialize(allow) & native
    kept = set(groups) if wildcard else (groups & allow)
    return _legacy_materialize(kept) & native


# ── Part 2: the intersection matrix (both entrypoints) ───────────────────────


def test_intersection_matrix_tier_aware_entrypoint():
    mism = {}
    for tname, budget in (("T1", BUDGETS["T1"]), ("T2", BUDGETS["T2"]), ("T3", BUDGETS["T3"])):
        for intent in INTENTS:
            for cx in COMPLEXITY_SIGNALS:
                got = _names(resolve_tools_for_tier(TOOLS, intent, cx, TAXONOMY, budget, mcp_allow=None))
                want = _legacy_oracle(intent, cx, budget)
                if got != want:
                    mism[f"{tname}|{intent}|{cx}"] = (sorted(got - want), sorted(want - got))
    assert not mism, f"tier-aware resolver diverges from legacy oracle in {len(mism)} cell(s): {dict(list(mism.items())[:6])}"


def test_intersection_matrix_tier_unaware_entrypoint():
    # resolve_tool_set is tier-unaware: the intent-only materialization (no tier
    # cap). Oracle = _materialize(intent_groups) ∩ native; None on unknown.
    native = {t["function"]["name"] for t in TOOLS}
    mism = {}
    for intent in INTENTS:
        for cx in COMPLEXITY_SIGNALS:
            got = resolve_tool_set(intent, cx, TAXONOMY)
            groups = _legacy_intent_groups(intent, cx)
            if groups is None:
                if got is not None:
                    mism[f"{intent}|{cx}"] = ("expected None", got)
                continue
            want = _legacy_materialize(set(groups)) & native
            got_native = {n for n in (got or set()) if n in native}
            if got_native != want:
                mism[f"{intent}|{cx}"] = (sorted(got_native - want), sorted(want - got_native))
    assert not mism, f"tier-unaware resolver diverges from legacy oracle in {len(mism)} cell(s): {dict(list(mism.items())[:6])}"


# ── Part 3: disclosure-mode invariants (the mechanism, in code) ──────────────

T3 = BUDGETS["T3"]


def test_complexity_record_absent_on_simple_t3_present_on_complex():
    # browser_navigate is a complexity record (exploratory cohort).
    simple = _names(resolve_tools_for_tier(TOOLS, "conversation", "simple", TAXONOMY, T3, mcp_allow=None))
    complex_ = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", TAXONOMY, T3, mcp_allow=None))
    assert "browser_navigate" not in simple, "complexity record leaked onto a low-complexity T3 turn"
    assert "browser_navigate" in complex_, "complexity record missing on a complex T3 turn"
    # ...and on a budgeted tier the exploratory group is not admitted at all.
    t1c = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", TAXONOMY, BUDGETS["T1"], mcp_allow=None))
    assert "browser_navigate" not in t1c


@pytest.mark.parametrize("fb_tool", ["spotify_search", "kanban_list", "discord", "computer_use"])
def test_fallback_record_absent_on_every_known_intent_present_only_in_fallback(fb_tool):
    # ABSENT on every known-intent cell (all tiers, all complexities), incl. the
    # hardest case T3|research|complex.
    for tname in ("T1", "T2", "T3"):
        b = BUDGETS[tname]
        for intent in INTENT_CLASSES:
            for cx in COMPLEXITY_SIGNALS:
                got = _names(resolve_tools_for_tier(TOOLS, intent, cx, TAXONOMY, b, mcp_allow=None))
                assert fb_tool not in got, f"fallback record {fb_tool} leaked onto known cell {tname}|{intent}|{cx}"
    # PRESENT only in the maximal unknown-intent fallback at the wildcard tier.
    t3_unknown = _names(resolve_tools_for_tier(TOOLS, None, "simple", TAXONOMY, T3, mcp_allow=None))
    assert fb_tool in t3_unknown, f"fallback record {fb_tool} missing from maximal unknown fallback"


def test_fallback_explicitly_t3_research_complex():
    # The named hardest case, stated directly.
    got = _names(resolve_tools_for_tier(TOOLS, "research", "complex", TAXONOMY, T3, mcp_allow=None))
    for fb in ("spotify_search", "kanban_list", "computer_use", "todo", "invoke_skill"):
        assert fb not in got, f"{fb} (fallback) must be absent on T3|research|complex"
